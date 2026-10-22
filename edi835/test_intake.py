"""Synthetic integration and failure-injection coverage for the shared boundary."""
import hashlib
import io
import json
import tempfile
import uuid
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, transaction
from django.test import Client as HttpClient, SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from accounts.models import Client, User
from .batch_jobs import import_legacy_jobs, queued_jobs, read_job, recover_interrupted_jobs, write_job
from .intake_service import current_transfer, register_sources
from .intake_storage import read_verified, source_path, store_stream
from .management.commands.run_batch_worker import Command as Worker
from .models import BatchDispatch, EDI837File, InboundTransfer, IntakePartner
from .test_edi837_service import SAMPLE_837


@override_settings(INTAKE_PARTNER_ENABLED=True, MFA_ENFORCEMENT_ENABLED=False)
class IntakeTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.options = override_settings(INTAKE_STORAGE_ROOT=Path(self.directory.name) / "private",
                                         MEDIA_ROOT=Path(self.directory.name) / "media")
        self.options.enable()
        self.addCleanup(self.options.disable)
        self.tenant = Client.objects.create(client_code="INTAKE", name="Synthetic", status="ACTIVE")
        self.other = Client.objects.create(client_code="OTHER", name="Other synthetic", status="ACTIVE")
        self.admin = User.objects.create_user(email="intake@example.test", name="Operator",
                                             mobile="+15550001111", password="Synthetic-test-only", is_staff=True, is_superuser=True)
        self.partner = IntakePartner.objects.create(client=self.tenant, name="Synthetic partner",
            token_digest=hashlib.sha256(b"synthetic-secret").hexdigest(), workflows=["837"],
            expires_at=timezone.now() + timedelta(days=1))
        self.authorization = f"Bearer {self.partner.pk}.synthetic-secret"

    def register(self, raw=b"source", **kwargs):
        options = dict(client=self.tenant, actor=self.admin, channel="STAFF", workflow="837",
                       sources=[("original", io.BytesIO(raw))], asynchronous=True)
        options.update(kwargs)
        return register_sources(**options)[0][0]

    def submit(self, raw=None, key="test-key", **kwargs):
        headers = dict(HTTP_AUTHORIZATION=self.authorization, HTTP_IDEMPOTENCY_KEY=key, secure=True)
        headers.update(kwargs)
        return self.client.post("/edi835/api/intake/partner/", {
            "workflow": "837", "file": SimpleUploadedFile("original", raw or SAMPLE_837.encode()),
        }, **headers)

    def test_receipt_hash_bytes_and_dispatch_committed_together(self):
        receipt = self.register(b"exact\r\nbytes")
        self.assertEqual(read_verified(receipt), b"exact\r\nbytes")
        self.assertEqual(receipt.checksum, hashlib.sha256(b"exact\r\nbytes").hexdigest())
        self.assertEqual(receipt.byte_count, 12)
        self.assertEqual(receipt.dispatch.state, "QUEUED")
        self.assertEqual(queued_jobs()[0]["id"], str(receipt.dispatch_id))
        self.assertEqual(receipt.actor, self.admin)

    def test_receipt_failure_rolls_back_dispatch_and_leaves_auditable_orphan(self):
        with patch.object(InboundTransfer.objects, "create", side_effect=DatabaseError("synthetic")):
            with self.assertRaises(DatabaseError):
                self.register()
        self.assertFalse(BatchDispatch.objects.exists())
        self.assertFalse(InboundTransfer.objects.exists())
        self.assertEqual(len(list(Path(settings.INTAKE_STORAGE_ROOT).glob("*/*.bin"))), 1)

    def test_queue_write_uses_database_transaction(self):
        job = {"id": str(uuid.uuid4()), "state": "QUEUED"}
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                write_job(job)
                raise RuntimeError("rollback")
        self.assertIsNone(read_job(job["id"]))

    def test_idempotent_retries_do_not_enqueue_twice(self):
        first = self.register(idempotency_key="retry")
        second = self.register(idempotency_key="retry")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(BatchDispatch.objects.count(), 1)
        self.assertEqual(len(list(Path(settings.INTAKE_STORAGE_ROOT).glob("*/*.bin"))), 1)

    def test_key_conflict_rejected_but_other_tenant_is_independent(self):
        self.register(idempotency_key="retry")
        with self.assertRaisesMessage(ValueError, "different content"):
            self.register(b"different", idempotency_key="retry")
        self.register(client=self.other, idempotency_key="retry")
        self.assertEqual(InboundTransfer.objects.count(), 2)

    def test_stream_reads_bounded_chunks_and_preserves_original_filename(self):
        class Stream(io.BytesIO):
            def read(self, size=-1):
                if not 0 < size <= 1024 * 1024:
                    raise AssertionError("unbounded read")
                return super().read(size)
        receipt = self.register(sources=[("../original.837", Stream(b"a" * (1024 * 1024 + 3)))])
        self.assertEqual(receipt.original_filename, "original.837")
        self.assertEqual(receipt.byte_count, 1024 * 1024 + 3)

    def test_oversize_and_broken_streams_never_leave_partial_files(self):
        with self.assertRaises(ValueError):
            store_stream(io.BytesIO(b"1234"), self.tenant.pk, max_bytes=3)
        class Broken:
            def read(self, size):
                raise OSError("disconnect")
        with self.assertRaises(OSError):
            store_stream(Broken(), self.tenant.pk, max_bytes=3)
        self.assertEqual(list(Path(settings.INTAKE_STORAGE_ROOT).glob("*/*")), [])

    def test_integrity_check_prevents_processing_tampered_source(self):
        receipt = self.register(SAMPLE_837.encode())
        source_path(receipt.storage_key).write_bytes(b"changed")
        Worker()._process(receipt.dispatch.payload)
        receipt.dispatch.refresh_from_db()
        self.assertEqual(receipt.dispatch.state, "FAILED")
        self.assertFalse(EDI837File.objects.exists())

    def test_partner_acceptance_then_real_837_worker_processing(self):
        response = self.submit()
        self.assertEqual(response.status_code, 202, response.content)
        receipt = InboundTransfer.objects.get()
        self.assertFalse(EDI837File.objects.exists())
        Worker()._process(receipt.dispatch.payload)
        receipt.dispatch.refresh_from_db()
        self.assertEqual(receipt.dispatch.state, "COMPLETED", receipt.dispatch.payload)
        self.assertEqual(EDI837File.objects.get().client_id, self.tenant.pk)
        self.assertEqual(InboundTransfer.objects.count(), 1)
        self.assertIsNone(current_transfer.get())

    def test_partner_requires_https_bearer_and_enabled_feature(self):
        for options in ({"secure": False}, {"HTTP_AUTHORIZATION": ""}, {"HTTP_AUTHORIZATION": "Bearer bad"}):
            with self.subTest(options=options):
                self.assertEqual(self.submit(**options).status_code, 401)
        with override_settings(INTAKE_PARTNER_ENABLED=False):
            self.assertEqual(self.submit().status_code, 401)
        self.assertFalse(InboundTransfer.objects.exists())

    def test_partner_cookie_only_requests_are_rejected(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.submit(HTTP_AUTHORIZATION="").status_code, 401)

    def test_revocation_and_expiry_checked_before_processing(self):
        receipt = self.register(actor=None, channel="PARTNER", partner=self.partner)
        self.partner.active = False
        self.partner.save()
        self.assertEqual(self.submit().status_code, 401)
        Worker()._process(receipt.dispatch.payload)
        receipt.dispatch.refresh_from_db()
        self.assertEqual(receipt.dispatch.state, "FAILED")
        self.assertFalse(EDI837File.objects.exists())
        self.partner.active = True
        self.partner.expires_at = timezone.now() - timedelta(seconds=1)
        self.partner.save()
        self.assertEqual(self.submit().status_code, 401)

    def test_workflow_restriction_and_tenant_status_scope(self):
        self.partner.workflows = ["835"]
        self.partner.save()
        self.assertEqual(self.submit().status_code, 403)
        receipt = self.register(client=self.other)
        response = self.client.get("/edi835/api/intake/partner/", {"receipt_id": str(receipt.pk)},
                                   HTTP_AUTHORIZATION=self.authorization, secure=True)
        self.assertEqual(response.status_code, 404)

    def test_database_failure_is_not_acknowledged(self):
        with patch("edi835.intake_api.register_sources", side_effect=DatabaseError("synthetic")):
            response = self.submit()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("synthetic", response.content.decode())

    def test_staff_requires_csrf_and_client_grant(self):
        browser = HttpClient(enforce_csrf_checks=True)
        browser.force_login(self.admin)
        response = browser.post("/edi835/api/intake/", {"client_id": str(self.tenant.pk)})
        self.assertEqual(response.status_code, 403)
        self.admin.is_superuser = False
        self.admin.save()
        self.client.force_login(self.admin)
        response = self.client.post("/edi835/api/intake/", {"client_id": str(self.tenant.pk),
            "workflow": "837", "file": SimpleUploadedFile("file", b"data")}, HTTP_IDEMPOTENCY_KEY="key")
        self.assertEqual(response.status_code, 403)

    def test_queue_capacity_and_offboarded_client_rejected(self):
        self.register()
        with override_settings(INTAKE_MAX_PENDING_PER_CLIENT=1):
            self.assertEqual(self.submit().status_code, 409)
        self.tenant.stage = "offboarded"
        self.tenant.save()
        self.assertEqual(self.submit().status_code, 401)

    def test_legacy_adoption_never_overwrites_database_state(self):
        from .batch_jobs import jobs_dir
        identifier = str(uuid.uuid4())
        job = {"id": identifier, "state": "QUEUED", "scope_key": "legacy"}
        (jobs_dir() / f"{identifier}.json").write_text(json.dumps(job))
        self.assertEqual(import_legacy_jobs(), 1)
        job["state"] = "COMPLETED"
        write_job(job)
        self.assertEqual(import_legacy_jobs(), 0)
        self.assertEqual(read_job(identifier)["state"], "COMPLETED")

    def test_worker_recovery_does_not_mark_live_inline_requests_failed(self):
        inline = self.register(asynchronous=False)
        queued = self.register()
        job = queued.dispatch.payload
        job["state"] = "RUNNING"
        write_job(job)
        self.assertEqual(recover_interrupted_jobs(), 1)
        inline.dispatch.refresh_from_db()
        self.assertEqual(inline.dispatch.state, "RUNNING")
        self.assertEqual(read_job(job["id"])["state"], "FAILED")

    def test_private_path_cannot_escape_storage(self):
        with self.assertRaises(ValueError):
            source_path("../../outside")

    def test_retry_with_existing_stored_object_does_not_delete_original(self):
        from .intake_storage import StoredSource
        first = self.register(idempotency_key="key")
        stored = StoredSource(first.storage_key, first.checksum, first.byte_count)
        second = self.register(sources=[("original", stored)], idempotency_key="key")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(read_verified(second), b"source")

    def test_legacy_combined_batch_has_one_dispatch_and_keeps_empty_refusal(self):
        from .intake_adapters import tracked_processing
        observed = []

        @tracked_processing("835", multiple=True)
        def processor(files_list, client, ingestion_source="SFTP"):
            observed.extend(files_list)
            return {"success": True}

        processor([{"filename": "a.835", "content": "a"},
                   {"filename": "b.835", "content": "b"},
                   {"filename": "empty.835", "content": ""}], self.tenant)
        self.assertEqual(len(observed), 3)
        self.assertEqual(InboundTransfer.objects.count(), 2)
        self.assertEqual(BatchDispatch.objects.count(), 1)
        self.assertEqual(BatchDispatch.objects.get().state, "COMPLETED")

    def test_queue_status_reads_database_and_detects_age(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        receipt = self.register()
        BatchDispatch.objects.filter(pk=receipt.dispatch_id).update(created_at=timezone.now() - timedelta(hours=1))
        output = io.StringIO()
        with self.assertRaises(CommandError):
            call_command("phase1_queue_status", max_queue_age_seconds=60,
                         fail_if_unhealthy=True, stdout=output)
        report = json.loads(output.getvalue())
        self.assertEqual(report["backend"], "database")
        self.assertEqual(report["counts"]["QUEUED"], 1)

    def test_staff_upload_is_accepted_and_keeps_exact_bytes(self):
        self.client.force_login(self.admin)
        response = self.client.post("/edi835/api/intake/", {
            "client_id": str(self.tenant.pk), "workflow": "837",
            "file": SimpleUploadedFile("file", b"\xef\xbb\xbf" + SAMPLE_837.encode()),
        }, HTTP_IDEMPOTENCY_KEY="staff-key")
        self.assertEqual(response.status_code, 202, response.content)
        receipt = InboundTransfer.objects.get()
        self.assertTrue(read_verified(receipt).startswith(b"\xef\xbb\xbf"))
        self.assertEqual(receipt.actor, self.admin)

    def test_partner_835_uses_existing_validator_and_converter(self):
        self.partner.workflows = ["835"]
        self.partner.save()
        raw = (Path(settings.BASE_DIR) / "edi835/fixtures/phase1/synthetic_paid.835").read_bytes()
        receipt = self.register(raw, workflow="835", actor=None, partner=self.partner, channel="PARTNER")
        with patch("edi835.services.process_edi835_file_content", return_value={"success": True}) as convert:
            with patch("converter.views._validate_835_for_conversion", return_value={"valid": True}) as validate:
                Worker()._process(receipt.dispatch.payload)
                validate.assert_called_once_with(raw.decode("utf-8-sig"))
        receipt.dispatch.refresh_from_db()
        self.assertEqual(receipt.dispatch.state, "COMPLETED", receipt.dispatch.payload)
        self.assertEqual(convert.call_args.kwargs["client"], self.tenant)
        self.assertEqual(convert.call_args.args[0], raw.decode("utf-8-sig"))

    def test_permanent_client_deletion_includes_new_intake_data(self):
        from accounts.client_deletion import permanently_delete_client, ClientDeletionError
        receipt = self.register()
        with self.assertRaises(ClientDeletionError):
            permanently_delete_client(actor=self.admin, client_id=self.tenant.pk,
                confirmation_name=self.tenant.name, password="Synthetic-test-only")
        job = receipt.dispatch.payload
        job["state"] = "FAILED"
        write_job(job)
        other_receipt = self.register(client=self.other)
        with self.captureOnCommitCallbacks(execute=True):
            permanently_delete_client(actor=self.admin, client_id=self.tenant.pk,
                confirmation_name=self.tenant.name, password="Synthetic-test-only")
        self.assertFalse(InboundTransfer.objects.filter(pk=receipt.pk).exists())
        self.assertFalse(source_path(receipt.storage_key).exists())
        self.assertFalse(IntakePartner.objects.filter(pk=self.partner.pk).exists())
        self.assertEqual(read_verified(other_receipt), b"source")

    def test_recon_upload_uses_receipt_boundary_and_preserves_bytes(self):
        from .models import RECONFile
        self.client.force_login(self.admin)
        raw = b"Claim ID,Member ID,Line Number,Procedure Code,Charge Amount,Paid Amount\nCLAIM-100,MEMBER-1,1,99213,100.00,80.00\n"
        response = self.client.post("/edi835/api/recon/upload/", {
            "client_id": str(self.tenant.pk),
            "recon_file": SimpleUploadedFile("recon.csv", raw),
        })
        self.assertEqual(response.status_code, 201, response.content)
        receipt = InboundTransfer.objects.get()
        self.assertEqual(receipt.workflow, "RECON")
        self.assertEqual(receipt.dispatch.state, "COMPLETED")
        self.assertEqual(read_verified(receipt), raw)
        self.assertEqual(RECONFile.objects.get().client_id, self.tenant.pk)

    def test_malformed_staff_client_id_returns_validation_error(self):
        self.client.force_login(self.admin)
        response = self.client.post("/edi835/api/intake/", {
            "client_id": "invalid", "workflow": "837",
            "file": SimpleUploadedFile("original", b"data"),
        }, HTTP_IDEMPOTENCY_KEY="key")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(InboundTransfer.objects.exists())

    def test_stale_worker_snapshot_cannot_execute_completed_job_again(self):
        receipt = self.register(SAMPLE_837.encode())
        snapshot = receipt.dispatch.payload
        Worker()._process(snapshot)
        Worker()._process(snapshot)
        self.assertEqual(EDI837File.objects.count(), 1)
        self.assertEqual(read_job(str(receipt.dispatch_id))["attempt_count"], 1)


class IntakeMigrationTests(SimpleTestCase):
    def test_upgrade_preserves_existing_data_and_can_be_applied_twice(self):
        source = '''
import django
django.setup()
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
call_command("migrate", "accounts", "0020_client_edi837_filename_format", verbosity=0)
old_apps = MigrationExecutor(connection).loader.project_state([("accounts", "0020_client_edi837_filename_format")]).apps
# The later retained filename column gets its DB default only on PostgreSQL.
# Insert the old tenant while its field is still present in SQLite's ORM state.
client = old_apps.get_model("accounts", "Client").objects.create(client_code="UPGRADE", name="Synthetic")
call_command("migrate", "edi835", "0057_analysis_comparison_index", verbosity=0)
apps = MigrationExecutor(connection).loader.project_state([("edi835", "0057_analysis_comparison_index")]).apps
apps.get_model("edi835", "EDI835File").objects.create(client_id=client.pk, original_filename="preserved.835", stored_filename="preserved.835")
call_command("migrate", "edi835", "0059_intake_constraints", verbosity=0)
apps = MigrationExecutor(connection).loader.project_state([("edi835", "0059_intake_constraints")]).apps
from django.utils import timezone
import uuid
for state in ("QUEUED", "RUNNING", "COMPLETED", "FAILED"):
    dispatch = apps.get_model("edi835", "BatchDispatch").objects.create(state=state, payload={"state": state})
    apps.get_model("edi835", "InboundTransfer").objects.create(
        client_id=client.pk, actor_reference="system", channel="SYSTEM", workflow="837",
        original_filename="old.837", storage_key=str(uuid.uuid4()), checksum="a" * 64,
        byte_count=1, received_started_at=timezone.now(), dispatch=dispatch)
call_command("migrate", "edi835", "0062_sftp_source_claims", verbosity=0)
apps = MigrationExecutor(connection).loader.project_state([("edi835", "0062_sftp_source_claims")]).apps
from edi835.test_client_portal_analysis import envelope
bpr = "*".join(["BPR", *([""] * 15), "20260914"])
upload = apps.get_model("edi835", "AnalysisUpload").objects.create(client_id=client.pk, kind="835", filename="old.835",
    content_sha256="c" * 64, raw_content=envelope("835", [bpr, "CLP*123*1*624*507*0*ZZ*REF"]).encode())
apps.get_model("edi835", "AnalysisClaim").objects.create(upload_id=upload.pk, sequence=1, claim_number="123",
    internal_number=" zz: ref ", billed="624", payment="507", patient_responsibility="0")
call_command("migrate", "edi835", "0063_analysis_payment_date", verbosity=0)
call_command("migrate", "edi835", "0063_analysis_payment_date", verbosity=0)
from edi835.models import AnalysisClaim
assert AnalysisClaim.objects.get().payment_date.isoformat() == "2026-09-14"
assert AnalysisClaim.objects.get().internal_number == "REF"
assert AnalysisClaim.objects.get().payment == 507
from edi835.models import EDI835File, InboundTransfer, BatchDispatch
assert EDI835File.objects.get().original_filename == "preserved.835"
assert set(InboundTransfer.objects.values_list("state", flat=True)) == {"QUEUED", "PROCESSING", "COMPLETED", "FAILED"}
assert set(InboundTransfer.objects.values_list("validation_outcome", flat=True)) == {"NOT_RECORDED"}
assert BatchDispatch.objects.count() == 4
try:
    call_command("migrate", "edi835", "0064_intake_dedup_provenance", verbosity=0)
except RuntimeError as exc:
    assert "Drain or review" in str(exc)
else:
    raise AssertionError("Live historical duplicate jobs must block migration")
BatchDispatch.objects.update(state="COMPLETED")
call_command("migrate", "edi835", "0064_intake_dedup_provenance", verbosity=0)
call_command("migrate", "edi835", "0064_intake_dedup_provenance", verbosity=0)
assert InboundTransfer.objects.filter(canonical_transfer__isnull=True).count() == 1
assert InboundTransfer.objects.count() == 4
call_command("migrate", "edi835", "0065_transfer_api_contract", verbosity=0)
call_command("migrate", "edi835", "0065_transfer_api_contract", verbosity=0)
from edi835.models import SubmissionKey, TransferRetry
assert SubmissionKey.objects.count() == 0
assert TransferRetry.objects.count() == 0
assert InboundTransfer.objects.count() == 4
MigrationExecutor(connection).loader.check_consistent_history(connection)
'''
        environment = dict(os.environ, SECRET_KEY="isolated-test-only", DATABASE_URL="sqlite:///:memory:",
                           ALLOW_SQLITE_FOR_TESTS="1", DEBUG="True", DJANGO_SETTINGS_MODULE="project835.settings")
        result = subprocess.run([sys.executable, "-c", source], cwd=settings.BASE_DIR,
                                env=environment, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
