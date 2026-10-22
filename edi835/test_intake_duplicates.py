"""File identity, tenant isolation and source lineage regression tests."""
import io
import json
from unittest.mock import Mock, patch

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from . import test_intake as fixtures
from .intake_adapters import tracked_processing
from .intake_lifecycle import record_validation
from .intake_service import register_sources
from .models import BatchDispatch, InboundTransfer, IntakeTransactionSet, ProcessingAttempt
from .test_client_portal_analysis import envelope


@override_settings(MFA_ENFORCEMENT_ENABLED=False, INTAKE_PARTNER_ENABLED=True)
class IntakeDuplicateTests(TestCase):
    setUp = fixtures.IntakeTests.setUp
    register = fixtures.IntakeTests.register
    submit = fixtures.IntakeTests.submit

    def test_same_bytes_different_channels_and_names_have_one_job(self):
        first = self.register()
        for channel in ('SFTP', 'PARTNER', 'STAFF'):
            duplicate = self.register(channel=channel, sources=[('renamed', io.BytesIO(b'source'))])
            self.assertEqual(duplicate.canonical_transfer_id, first.pk)
            self.assertEqual(duplicate.state, 'DUPLICATE')
            self.assertEqual(duplicate.dispatch.state, 'COMPLETED')
            self.assertFalse(duplicate.dispatch.attempts.exists())
        self.assertEqual(BatchDispatch.objects.filter(state='QUEUED').count(), 1)

    def test_tenant_workflow_and_corrected_business_content_are_independent(self):
        original = envelope('835', ['CLP*CLAIM*1*100*80*20*ZZ*REF']).encode()
        for content in (original, original.replace(b'*80*', b'*70*'), original.replace(b'*1*100*80*', b'*22*-100*-80*')):
            self.assertIsNone(self.register(content, workflow='835').canonical_transfer_id)
        self.assertIsNone(self.register(original, workflow='835', client=self.other).canonical_transfer_id)
        self.assertIsNone(self.register(original, workflow='837').canonical_transfer_id)
        self.assertIsNone(self.register(original, workflow='ANALYSIS_835', asynchronous=False).canonical_transfer_id)

    def test_idempotent_retry_returns_same_duplicate_receipt(self):
        self.register()
        first = self.register(idempotency_key='duplicate-retry')
        second = self.register(idempotency_key='duplicate-retry')
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(InboundTransfer.objects.count(), 2)

    def test_database_rejects_a_second_active_canonical_receipt(self):
        first = self.register()
        with self.assertRaises(IntegrityError), transaction.atomic():
            InboundTransfer.objects.create(client=self.tenant, workflow='837', channel='STAFF',
                checksum=first.checksum, byte_count=1, storage_key='different',
                received_started_at=first.received_at, dispatch=first.dispatch)

    def test_mixed_batch_only_processes_new_sources(self):
        self.register(b'existing', workflow='835')
        seen = []

        @tracked_processing('835', multiple=True)
        def process(files_list, client):
            seen.extend(files_list)
            return {'success': True}

        result = process([{'filename': 'same.835', 'content': 'existing'},
                          {'filename': 'new.835', 'content': 'new'},
                          {'filename': 'repeat.835', 'content': 'new'}], client=self.tenant)
        self.assertEqual(seen, [{'filename': 'new.835', 'content': 'new'}])
        self.assertEqual(len(result['receipts']), 3)
        self.assertEqual(ProcessingAttempt.objects.count(), 1)

    def test_content_guard_rejects_renamed_archives_and_executables(self):
        for raw in (b'PK\x03\x04hidden', b'MZhidden', b'\x7fELFhidden', b'\x1f\x8bhidden'):
            with self.subTest(raw=raw), self.assertRaisesMessage(ValueError, 'Unsupported file content'):
                self.register(raw)
        self.assertFalse(InboundTransfer.objects.exists())
        with override_settings(INTAKE_MAX_REQUEST_BYTES=2), self.assertRaises(ValueError):
            self.register(b'123')

    def test_transaction_sets_have_stable_coordinates_and_no_claim_data(self):
        raw = envelope('835', ['CLP*CLAIM*1*100*80*20*ZZ*REF']).encode()
        receipt = self.register(raw, workflow='835')
        record_validation(receipt, raw)
        record_validation(receipt, raw)
        part = IntakeTransactionSet.objects.get()
        self.assertEqual(part.receipt, receipt)
        self.assertEqual(part.transaction_type, '835')
        self.assertGreater(part.last_segment, part.first_segment)
        self.assertEqual(len(part.checksum), 64)

    def test_partner_duplicate_has_own_status_and_cannot_read_staff_receipt(self):
        first = self.submit().json()
        second = self.submit(key='second')
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()['duplicate'])
        self.assertEqual(second.json()['canonical_receipt_id'], first['receipt_id'])
        unrelated = self.register(b'other-content')
        response = self.client.get(f'/edi835/api/intake/partner/?receipt_id={unrelated.pk}',
            HTTP_AUTHORIZATION=self.authorization, secure=True)
        self.assertEqual(response.status_code, 404)

    def test_failed_mir_is_not_silently_reconverted(self):
        first = self.register(workflow='835')
        InboundTransfer.objects.filter(pk=first.pk).update(state='FAILED')
        self.assertEqual(self.register(workflow='835').canonical_transfer_id, first.pk)

    def test_async_conversion_preserves_original_validation_bytes(self):
        from .models import EDI835File
        from .intake_service import finish_inline, current_transfer
        from .intake_storage import read_verified
        from .management.commands.run_batch_worker import Command
        raw = b'\xef\xbb\xbf' + envelope('835', ['CLP*CLAIM*1*100*80*20*ZZ*REF']).encode() + b'\r\n'
        source = EDI835File.objects.create(client=self.tenant, original_filename='source.835',
            stored_filename='source.835', input_file_content=raw.decode('utf-8-sig').strip())
        validated = self.register(raw, workflow='VALIDATE_835', asynchronous=False)
        finish_inline([validated], True, source_id=source.pk)
        self.client.force_login(self.admin)
        response = self.client.post('/api/convert-async/', json.dumps({'file_id': str(source.pk)}), content_type='application/json')
        self.assertEqual(response.status_code, 202, response.content)
        receipt = InboundTransfer.objects.get(workflow='835')
        self.assertEqual(read_verified(receipt), raw)
        self.assertEqual(response.json()['receipts'][0]['receipt_id'], str(receipt.pk))
        self.assertEqual(self.register(raw, workflow='835', channel='SFTP').canonical_transfer_id, receipt.pk)

        def process(*args):
            self.assertEqual(current_transfer.get().pk, receipt.pk)
            return 400, {'success': False, 'error': 'Synthetic processing rejection'}

        with patch.object(Command, '_manual_conversion', side_effect=process):
            Command()._process(receipt.dispatch.payload)
        receipt.refresh_from_db()
        self.assertEqual(receipt.state, 'FAILED')
        self.assertEqual(receipt.dispatch.payload['result']['receipts'][0]['lifecycle_state'], 'FAILED')

    def test_http_mixed_batch_filters_duplicates_before_legacy_handler(self):
        from django.http import JsonResponse
        from django.test import RequestFactory
        from .intake_adapters import track_upload_request
        self.register(b'old', workflow='835')
        seen = []

        @track_upload_request('CONVERT_835')
        def convert(request):
            seen.extend(json.loads(request.body)['files'])
            return JsonResponse({'success': True})

        request = RequestFactory().post('/api/convert/', json.dumps({'client_id': str(self.tenant.pk),
            'files': [{'filename': 'old.835', 'content': 'old'}, {'filename': 'new.835', 'content': 'new'}]}),
            content_type='application/json')
        request.user = self.admin
        response = convert(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, [{'filename': 'new.835', 'content': 'new'}])
        self.assertEqual([item['lifecycle_state'] for item in json.loads(response.content)['receipts']], ['DUPLICATE', 'COMPLETED'])

    def test_output_lineage_excludes_rejected_batch_sources(self):
        from . import test_intake_recovery as recovery
        from .batch_jobs import claim_job
        raw = envelope('835', ['CLP*CLAIM*1*100*80*20*ZZ*REF']).encode()
        receipt = self.register(raw, workflow='835')
        record_validation(receipt, raw, outcome='VALID')
        rejected_raw = raw.replace(b'CLAIM', b'REJECTED')
        rejected = self.register(rejected_raw, workflow='835')
        InboundTransfer.objects.filter(pk=rejected.pk).update(dispatch=receipt.dispatch)
        job = claim_job(str(receipt.dispatch_id))
        record_validation(rejected, rejected_raw, outcome='REJECTED')
        output, _, _ = recovery.IntakeRecoveryTests.output(self, receipt, job)
        self.assertEqual(list(output.source_transactions.values_list('receipt_id', flat=True)), [receipt.pk])

    def test_declared_request_size_rejected_before_body_is_read(self):
        from django.test import RequestFactory
        from project835.middleware import IntakeBodyLimitMiddleware
        downstream = Mock()
        request = RequestFactory().post('/api/validate/', data=b'{}', content_type='application/json')
        request.META['CONTENT_LENGTH'] = str(129 * 1024 * 1024)
        response = IntakeBodyLimitMiddleware(downstream)(request)
        self.assertEqual(response.status_code, 413)
        downstream.assert_not_called()

    def test_demo_receipt_read_is_limited_to_assigned_analysis_client(self):
        from accounts.models import User
        user = User.objects.create_user(email='demo-receipt@example.test', name='Demo', mobile='+15550002222',
            client=self.tenant, client_role='DEMO', password='Synthetic-test-only')
        own = self.register(workflow='ANALYSIS_837', asynchronous=False)
        foreign = self.register(client=self.other, workflow='ANALYSIS_837', asynchronous=False)
        production = self.register()
        self.client.force_login(user)
        for receipt, status in ((own, 200), (foreign, 404), (production, 404)):
            response = self.client.get(f'/edi835/api/client-portal/analysis/?view=receipt&receipt_id={receipt.pk}')
            self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(self.client.get(f'/edi835/api/intake/?receipt_id={production.pk}').status_code, 403)
