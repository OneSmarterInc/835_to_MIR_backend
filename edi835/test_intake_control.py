import io
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings, Client as HttpClient
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from admin_panel.models import AuditLog
from . import test_intake as fixtures
from .models import (IntakeStaffGrant, InboundTransfer, IntakeQuarantine, IntakeLegalHold,
    IntakeRetentionPolicy, IntakeArtifactDisposition, IntakeOperatingTargets, IntakeRecoveryReview)
from .intake_control import apply_retention, eligible_artifacts, ensure_quarantine
from .intake_storage import source_path, open_verified
from .batch_jobs import claim_job, write_job
from .partner_management import change_partner


@override_settings(ROOT_URLCONF="project835.urls", INTAKE_PARTNER_ENABLED=True, MFA_ENFORCEMENT_ENABLED=False)
class IntakeControlTests(TestCase):
    register = fixtures.IntakeTests.register

    def setUp(self):
        fixtures.IntakeTests.setUp(self)
        self.client.force_login(self.admin)
        self.url = '/admin-panel/api/intake-control/'

    def post(self, action, **data):
        return self.client.post(self.url, json.dumps({'action': action, 'reason': 'Synthetic approved change', **data}), content_type='application/json')

    def grant(self, download=False, retry=False):
        return IntakeStaffGrant.objects.create(user=self.admin, client=self.tenant, can_download=download,
            can_retry=retry, approved_by=self.admin, reason='Synthetic scope', expires_at=timezone.now()+timedelta(days=1))

    def test_dashboard_real_counts_no_filename_or_claim_content(self):
        receipt = self.register(b'synthetic source')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['counts']['QUEUED'], 1)
        self.assertEqual(response.json()['rows'][0]['id'], str(receipt.pk))
        self.assertNotIn('original', response.content.decode())
        self.assertIsNone(response.json()['targets'])

    def test_bootstrap_and_partner_metadata(self):
        self.assertEqual(self.client.get(self.url, {'tab': 'bootstrap'}).status_code, 200)
        response = self.client.get(self.url, {'tab': 'partners'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn('synthetic-secret', response.content.decode())

    def test_partner_metadata_reports_route_enforcement_switch(self):
        for enabled in (False, True):
            with override_settings(INTAKE_ENFORCE_PARTNER_ROUTES=enabled):
                response = self.client.get(self.url, {'tab': 'partners'})
                self.assertIs(response.json()['rows'][0]['route_enforcement_enabled'], enabled)

    def test_authentication_edit_preserves_saved_routing_preference(self):
        self.partner.routing_required = False
        self.partner.save(update_fields=['routing_required'])
        self.post('partner', partner_id=str(self.partner.pk), operation='POLICY', data={'auth_mode': 'TOKEN'})
        self.partner.refresh_from_db()
        self.assertFalse(self.partner.routing_required)

    def preview(self, raw, client=None):
        return (client or self.client).post(self.url, {'action': 'route_preview',
            'partner_id': str(self.partner.pk), 'file': SimpleUploadedFile('extensionless', raw)})

    def test_route_preview_extracts_both_workflows_without_intake_or_route_writes(self):
        from django.conf import settings
        from .models import PartnerRoute, BatchDispatch
        self.partner.workflows = ['835', '837']
        self.partner.save()
        for workflow, filename in [('837', 'synthetic_claim.837'), ('835', 'synthetic_payment.835')]:
            response = self.preview((settings.BASE_DIR / 'examples' / filename).read_bytes())
            self.assertEqual(response.status_code, 200, response.content)
            route = response.json()['routes'][0]
            self.assertEqual(route['workflow'], workflow)
            self.assertEqual(route['isa_sender'], 'SYNTHETIC')
            self.assertEqual(route['isa_receiver'], 'TESTONLY')
            self.assertEqual(response['Cache-Control'], 'private, no-store')
            self.assertNotIn('CLM*', response.content.decode())
            self.assertNotIn('CLP*', response.content.decode())
        self.assertFalse(PartnerRoute.objects.exists())
        self.assertFalse(InboundTransfer.objects.exists())
        self.assertFalse(BatchDispatch.objects.exists())
        self.assertEqual(AuditLog.objects.filter(action='ROUTE_PREVIEW').count(), 2)

    def test_route_preview_deduplicates_headers_and_returns_all_distinct_routes(self):
        from django.conf import settings
        raw = (settings.BASE_DIR / 'examples/synthetic_claim.837').read_bytes()
        response = self.preview(raw + raw)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()['routes']), 1)
        self.assertEqual(response.json()['transaction_set_count'], 2)
        response = self.preview(raw + raw.replace(b'TESTONLY', b'OTHERTEN'))
        self.assertEqual(len(response.json()['routes']), 2)

    def test_preview_full_835_and_837_together_then_atomic_approval(self):
        from django.conf import settings
        from .models import PartnerRoute, BatchDispatch
        self.partner.workflows = ['835', '837']
        self.partner.save()
        # Valid multi-interchange files larger than the JSON command-body limit.
        payment = (settings.BASE_DIR / 'examples/synthetic_payment.835').read_bytes() * 150
        claim = (settings.BASE_DIR / 'examples/synthetic_claim.837').read_bytes() * 150
        self.assertGreater(len(payment), 65536)
        self.assertGreater(len(claim), 65536)
        result = self.client.post(self.url, {'action': 'route_preview', 'partner_id': str(self.partner.pk),
            'file_835': SimpleUploadedFile('full-payment.835', payment),
            'file_837': SimpleUploadedFile('full-claim.837', claim)})
        self.assertEqual(result.status_code, 200, result.content)
        routes = result.json()['routes']
        self.assertEqual([r['workflow'] for r in routes], ['835', '837'])
        self.assertEqual(result.json()['transaction_set_count'], 300)
        self.assertFalse(PartnerRoute.objects.exists())
        self.assertFalse(InboundTransfer.objects.exists())
        self.assertFalse(BatchDispatch.objects.exists())
        bad = self.post('partner', partner_id=str(self.partner.pk), operation='ROUTES',
            data={'client_id': str(self.tenant.pk), 'routes': [routes[0], {**routes[1], 'workflow': '999'}]})
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(PartnerRoute.objects.exists(), 'A failed batch must not partly approve routes')
        good = self.post('partner', partner_id=str(self.partner.pk), operation='ROUTES',
            data={'client_id': str(self.tenant.pk), 'routes': routes})
        self.assertEqual(good.status_code, 200, good.content)
        self.assertEqual(PartnerRoute.objects.count(), 2)

    def test_preview_rejects_wrong_file_slot_and_duplicate_parts(self):
        from django.conf import settings
        raw = (settings.BASE_DIR / 'examples/synthetic_claim.837').read_bytes()
        for files in [{'file_835': SimpleUploadedFile('wrong.835', raw)},
                      {'file_837': [SimpleUploadedFile('a.837', raw), SimpleUploadedFile('b.837', raw)]}]:
            response = self.client.post(self.url, {'action': 'route_preview', 'partner_id': str(self.partner.pk), **files})
            self.assertEqual(response.status_code, 400, response.content)

    def test_route_preview_rejects_truncated_mixed_and_unsupported_workflows(self):
        from django.conf import settings
        raw = (settings.BASE_DIR / 'examples/synthetic_claim.837').read_bytes()
        payment = (settings.BASE_DIR / 'examples/synthetic_payment.835').read_bytes()
        for invalid in [b'', b'not edi', raw[:-25], raw+b'extra', raw+payment]:
            self.assertEqual(self.preview(invalid).status_code, 400)
        self.partner.workflows = ['835']
        self.partner.save()
        self.assertEqual(self.preview(raw).status_code, 400)

    def test_route_preview_requires_screen_access_and_csrf(self):
        self.assertEqual(self.preview(b'sample', HttpClient()).status_code, 403)
        csrf_client = HttpClient(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(self.preview(b'sample', csrf_client).status_code, 403)
        self.admin.is_superuser = False
        self.admin.admin_screens = ['ops']
        self.admin.save()
        self.assertEqual(self.preview(b'sample').status_code, 403)

    def test_route_preview_enforces_size_before_reading(self):
        from .route_preview import extract_routes, MAX_PREVIEW_BYTES
        from types import SimpleNamespace
        oversized = SimpleNamespace(size=MAX_PREVIEW_BYTES+1, read=lambda *_: self.fail('Oversized file read'))
        with self.assertRaisesMessage(ValueError, '10 MiB'):
            extract_routes(oversized, ['835', '837'])

    def test_non_admin_and_unassigned_screen_denied(self):
        self.admin.is_superuser = False
        self.admin.admin_screens = ['ops']
        self.admin.save()
        self.assertEqual(self.client.get(self.url, {'tab': 'partners'}).status_code, 403)
        self.assertEqual(self.client.get(self.url, {'tab': 'operations'}).status_code, 200)
        self.assertEqual(self.post('create_partner').status_code, 403)

    def test_csrf_required(self):
        client = HttpClient(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(self.url, json.dumps({'action': 'targets'}), content_type='application/json').status_code, 403)

    def test_superadmin_requires_explicit_read_and_download(self):
        receipt = self.register()
        url = f'/api/v1/transfers/{receipt.pk}'
        self.assertEqual(self.client.get(url).status_code, 404)
        grant = self.grant()
        detail = self.client.get(url)
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn('source_url', detail.json())
        self.assertEqual(self.client.get(url+'/source').status_code, 403)
        grant.can_download = True
        grant.save()
        response = self.client.get(url+'/source')
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertTrue(AuditLog.objects.filter(action='RECEIPT_DETAIL').exists())
        self.assertTrue(AuditLog.objects.filter(action='FILE_DOWNLOAD', details__contains='ALLOWED').exists())
        grant.revoked_at = timezone.now()
        grant.save()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_foreign_client_not_exposed_by_grant(self):
        self.grant(download=True)
        receipt = self.register(client=self.other)
        self.assertEqual(self.client.get(f'/api/v1/transfers/{receipt.pk}').status_code, 404)
        self.assertEqual(self.client.get('/api/v1/transfers').json()['total'], 0)

    def test_grant_and_targets_audited(self):
        response = self.post('grant', user_id=str(self.admin.pk), client_id=str(self.tenant.pk), days=1, can_download=True, can_retry=False)
        self.assertEqual(response.status_code, 200, response.content)
        response = self.post('targets', queue_age_seconds=120, rpo_seconds=3600, rto_seconds=7200, evidence_reference='APPROVAL-001')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(IntakeOperatingTargets.objects.get().rpo_seconds, 3600)
        self.assertTrue(AuditLog.objects.filter(action='TARGETS_APPROVED').exists())

    def test_partner_client_scope_requires_context(self):
        change_partner(self.partner.pk, action='CLIENTS', actor='test', reason='Approved scopes', data={'client_ids': [str(self.tenant.pk), str(self.other.pk)]})
        headers = dict(secure=True, HTTP_AUTHORIZATION=self.authorization, HTTP_IDEMPOTENCY_KEY='scope-key')
        def upload(**data):
            return self.client.post('/api/v1/transfers', {'workflow': '837', 'file': SimpleUploadedFile('file', b'valid fixture'), **data}, **headers)
        self.assertEqual(upload().status_code, 400)
        response = upload(client_id=str(self.other.pk))
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(InboundTransfer.objects.get().client, self.other)
        change_partner(self.partner.pk, action='CLIENTS', actor='test', reason='Scope withdrawn', data={'client_ids': [str(self.tenant.pk)]})
        self.assertEqual(self.client.get(response.json()['status_url'], secure=True, HTTP_AUTHORIZATION=self.authorization).status_code, 404)

    def test_partner_rate_storage_and_concurrency(self):
        self.partner.requests_per_minute = 1
        self.partner.storage_limit_bytes = 2
        self.partner.save()
        def upload(key):
            return self.client.post('/api/v1/transfers', {'workflow': '837', 'file': SimpleUploadedFile('file', b'too big')}, secure=True,
                HTTP_AUTHORIZATION=self.authorization, HTTP_IDEMPOTENCY_KEY=key)
        self.assertEqual(upload('one').status_code, 429)
        self.assertEqual(upload('two').json()['code'], 'PARTNER_RATE_LIMIT')
        self.partner.storage_limit_bytes, self.partner.concurrent_jobs = 100, 1
        self.partner.save()
        a = self.register(b'first', partner=self.partner, channel='PARTNER')
        b = self.register(b'second', partner=self.partner, channel='PARTNER')
        self.assertIsNotNone(claim_job(str(a.dispatch_id)))
        self.assertIsNone(claim_job(str(b.dispatch_id)))

    def test_quarantine_review_does_not_accept_invalid_content(self):
        receipt = self.register()
        job = claim_job(str(receipt.dispatch_id))
        job.update(state='FAILED', receipt_state='REJECTED', safe_error_code='VALIDATION_REJECTED')
        write_job(job)
        self.assertEqual(IntakeQuarantine.objects.get().state, 'OPEN')
        self.assertEqual(self.post('quarantine_review', receipt_id=str(receipt.pk)).status_code, 200)
        receipt.refresh_from_db()
        self.assertEqual(receipt.state, 'REJECTED')
        self.assertEqual(self.post('quarantine_revalidate', receipt_id=str(receipt.pk)).status_code, 403)
        self.grant(retry=True)
        response = self.post('quarantine_revalidate', receipt_id=str(receipt.pk))
        self.assertEqual(response.status_code, 200, response.content)
        receipt.refresh_from_db()
        self.assertEqual(receipt.state, 'QUEUED')

    def old_receipt(self):
        receipt = self.register()
        job = claim_job(str(receipt.dispatch_id))
        job.update(state='COMPLETED', result={'success': True})
        write_job(job)
        InboundTransfer.objects.filter(pk=receipt.pk).update(received_at=timezone.now()-timedelta(days=30))
        return receipt

    def policy(self):
        return IntakeRetentionPolicy.objects.create(client=self.tenant, source_days=10, approved_by=self.admin, evidence_reference='POLICY-001')

    def test_retention_default_retain_and_hold_blocks_purge(self):
        receipt = self.old_receipt()
        self.assertEqual(eligible_artifacts(self.tenant), [])
        self.policy()
        hold = IntakeLegalHold.objects.create(client=self.tenant, reference='HOLD-1', created_by=self.admin)
        self.assertEqual(apply_retention(self.tenant.pk), 0)
        self.assertTrue(source_path(receipt.storage_key).exists())
        hold.released_at = timezone.now()
        hold.save()
        self.assertEqual(apply_retention(self.tenant.pk), 1)
        self.assertFalse(source_path(receipt.storage_key).exists())
        self.assertTrue(InboundTransfer.objects.filter(pk=receipt.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action='ARTIFACT_PURGED').exists())

    def test_pending_retention_respects_new_hold_and_extended_policy(self):
        receipt = self.old_receipt()
        policy = self.policy()
        IntakeArtifactDisposition.objects.create(storage_key=receipt.storage_key, client=self.tenant, kind='SOURCE')
        hold = IntakeLegalHold.objects.create(client=self.tenant, receipt=receipt, reference='HOLD', created_by=self.admin)
        self.assertEqual(apply_retention(self.tenant.pk), 0)
        hold.released_at = timezone.now(); hold.save()
        policy.source_days = 90; policy.save()
        self.assertEqual(apply_retention(self.tenant.pk), 0)
        self.assertTrue(source_path(receipt.storage_key).exists())

    def test_recovery_review_does_not_resend_or_change_completion(self):
        receipt = self.register()
        response = self.post('recovery_review', receipt_id=str(receipt.pk), decision='NO_DELIVERY_CONFIRMED', evidence_reference='TICKET-123')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(IntakeRecoveryReview.objects.count(), 1)
        receipt.refresh_from_db()
        self.assertEqual(receipt.state, 'QUEUED')
        self.grant()
        detail = self.client.get(f'/api/v1/transfers/{receipt.pk}').json()
        self.assertEqual(detail['recovery_reviews'][0]['evidence_reference'], 'TICKET-123')

    def test_idempotency_recovery_survives_reduced_storage_quota(self):
        def upload():
            return self.client.post('/api/v1/transfers', {'workflow': '837',
                'file': SimpleUploadedFile('file', b'original accepted content')}, secure=True,
                HTTP_AUTHORIZATION=self.authorization, HTTP_IDEMPOTENCY_KEY='quota-replay')
        first = upload()
        self.assertEqual(first.status_code, 202, first.content)
        self.partner.storage_limit_bytes = 1
        self.partner.save(update_fields=['storage_limit_bytes'])
        replay = upload()
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(first.json()['receipt_id'], replay.json()['receipt_id'])
        self.assertEqual(InboundTransfer.objects.count(), 1)

    def test_original_client_can_be_removed_without_moving_receipts(self):
        receipt = self.register(partner=self.partner, channel='PARTNER')
        change_partner(self.partner.pk, action='CLIENTS', actor='test', reason='Reassign permitted client',
            data={'client_ids': [str(self.other.pk)]})
        from .partner_auth import client_ids, resolve_client
        self.partner.refresh_from_db()
        self.assertEqual(set(client_ids(self.partner)), {self.other.pk})
        self.assertEqual(resolve_client(self.partner), self.other)
        receipt.refresh_from_db()
        self.assertEqual(receipt.client_id, self.tenant.pk)

    @override_settings(INTAKE_ENFORCE_PARTNER_ROUTES=True)
    def test_multi_client_envelope_route_must_match_requested_client(self):
        from django.conf import settings
        from .partner_routing import ROUTE_FIELDS, envelope_routes, authorize_routes, EnvelopeError
        from .intake_storage import store_stream
        raw = (settings.BASE_DIR / 'examples/synthetic_claim.837').read_bytes()
        change_partner(self.partner.pk, action='CLIENTS', actor='test', reason='Approve two clients',
            data={'client_ids': [str(self.tenant.pk), str(self.other.pk)]})
        route = dict(zip(ROUTE_FIELDS, next(envelope_routes(io.BufferedReader(io.BytesIO(raw)), '837'))))
        change_partner(self.partner.pk, action='ROUTE', actor='test', reason='Approve envelope route',
            data={**route, 'workflow': '837', 'client_id': str(self.other.pk)})
        self.partner.routing_required = True
        self.partner.save()
        source = store_stream(io.BytesIO(raw), self.other.pk, max_bytes=len(raw))
        self.assertEqual(len(authorize_routes(self.partner, '837', source, self.other)), 1)
        with self.assertRaises(EnvelopeError):
            authorize_routes(self.partner, '837', source, self.tenant)

    def test_expired_grant_is_not_authorization(self):
        receipt = self.register()
        grant = self.grant(download=True, retry=True)
        grant.expires_at = timezone.now() - timedelta(seconds=1)
        grant.save()
        self.assertEqual(self.client.get(f'/api/v1/transfers/{receipt.pk}').status_code, 404)

    def test_displayed_storage_matches_enforcement_with_shared_dispatch(self):
        from .models import IntakeOutput, EDI835File
        from .intake_control import partner_storage_bytes, partner_storage_totals
        from .partner_management import create_partner
        other_partner = create_partner(client=self.tenant, name='Second partner', workflows=['837'],
            mode='TOKEN', routing_required=False, actor='test', reason='Synthetic storage accounting')
        change_partner(other_partner.pk, action='PERMISSIONS', actor='test', reason='Synthetic scope',
            data={'permissions': [{'action': 'SUBMIT', 'workflow': '837'}]})
        receipt = self.register(b'shared source', partner=self.partner, channel='PARTNER')
        duplicate = self.register(b'shared source', partner=other_partner, channel='PARTNER')
        # Model a shared batch checkpoint, which must be counted once per owner.
        InboundTransfer.objects.filter(pk=duplicate.pk).update(dispatch_id=receipt.dispatch_id)
        source = EDI835File.objects.create(client=self.tenant, original_filename='synthetic', stored_filename='synthetic')
        IntakeOutput.objects.create(dispatch=receipt.dispatch, source_835=source,
            storage_key='synthetic-output', checksum='0'*64, byte_count=30, converter_version='synthetic')
        totals = partner_storage_totals([self.partner.pk, other_partner.pk])
        for partner in [self.partner, other_partner]:
            self.assertEqual(totals[partner.pk], partner_storage_bytes(partner))
            self.assertEqual(totals[partner.pk], len(b'shared source')+30)

    def test_legacy_download_audits_identifiers_without_filename(self):
        from django.http import FileResponse
        from django.test import RequestFactory
        from types import SimpleNamespace
        from .intake_audit_middleware import DownloadAuditMiddleware
        request = RequestFactory().get('/synthetic/download')
        request.user = self.admin
        request.resolver_match = SimpleNamespace(view_name='synthetic-download', kwargs={'file_id': 12, 'filename': 'private-person.x12'})
        response = DownloadAuditMiddleware(lambda _: FileResponse(io.BytesIO(b'private bytes'), as_attachment=True))(request)
        response.close()
        record = AuditLog.objects.get(action='LEGACY_FILE_DOWNLOAD')
        self.assertIn('12', record.details)
        self.assertNotIn('private-person', record.details)
        self.assertNotIn('private bytes', record.details)
        self.assertIn('X-Request-ID', response)
