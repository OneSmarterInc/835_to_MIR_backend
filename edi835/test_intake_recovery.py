import io
import json
import os
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from . import test_intake as fixtures
from .batch_jobs import claim_job, read_job, write_job
from .intake_lifecycle import LeaseLost, begin_effects, execution, record_validation, recover_expired, renew
from .intake_outputs import checkpoint_output, delivery_checkpoint, replay_output, verify_output
from .intake_service import current_transfer, finish_inline
from .intake_storage import source_path
from .management.commands.run_batch_worker import Command as Worker
from .mir_persistence import store_mir_file
from .models import BatchDispatch, EDI835File, InboundTransfer, IntakeOutput, MIRFile, ProcessingAttempt
from .storage import write_mir_copies


@override_settings(ROOT_URLCONF="project835.urls", INTAKE_PARTNER_ENABLED=True, INTAKE_LEASE_SECONDS=60)
class IntakeRecoveryTests(TestCase):
    setUp = fixtures.IntakeTests.setUp
    register = fixtures.IntakeTests.register

    def expired(self, job):
        ProcessingAttempt.objects.filter(pk=job["attempt_id"]).update(lease_expires_at=timezone.now() - timedelta(seconds=1))

    def output(self, receipt, job):
        header = list(" " * 334)
        header[248:252] = "0101"
        header[332:334] = "01"
        text = "".join(header) + " " * 303 + "\n"
        source = EDI835File.objects.create(client=self.tenant, original_filename="synthetic.835",
            stored_filename="synthetic.835", claims_count=1, delivered_claims_count=1)
        mir = store_mir_file(source_835=source, mir_filename="synthetic.MIR", mir_text=text)
        archive, outbound = write_mir_copies(self.tenant, "synthetic.MIR", text)
        token = current_transfer.set(receipt)
        try:
            with execution(job):
                begin_effects()
                checkpoint_output(source, mir, archive, outbound)
        finally:
            current_transfer.reset(token)
        return IntakeOutput.objects.get(dispatch=receipt.dispatch), text, source

    def command(self, *args):
        output = io.StringIO()
        call_command("intake_reconcile", *args, stdout=output)
        return json.loads(output.getvalue())

    def test_claim_creates_one_fenced_attempt_and_renewal_extends_lease(self):
        receipt = self.register()
        job = claim_job(str(receipt.dispatch_id))
        self.assertIsNone(claim_job(job["id"]))
        attempt = ProcessingAttempt.objects.get()
        old = attempt.lease_expires_at
        with patch("edi835.intake_lifecycle.timezone.now", return_value=timezone.now() + timedelta(seconds=10)):
            self.assertTrue(renew(job))
        attempt.refresh_from_db()
        self.assertGreater(attempt.lease_expires_at, old)
        receipt.refresh_from_db()
        self.assertEqual(receipt.state, "VALIDATING")
        self.assertEqual(len(attempt.converter_version), 64)

    def test_database_forbids_two_live_attempts(self):
        receipt = self.register()
        claim_job(str(receipt.dispatch_id))
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProcessingAttempt.objects.create(dispatch=receipt.dispatch, sequence=2,
                lease_expires_at=timezone.now() + timedelta(minutes=1), converter_version="synthetic")

    def test_expired_pre_effect_job_retries_with_parent_and_fences_stale_writer(self):
        receipt = self.register()
        old = claim_job(str(receipt.dispatch_id))
        self.expired(old)
        self.assertEqual(recover_expired()["requeued"], 1)
        self.assertEqual(read_job(old["id"])["state"], "RUNNING")
        self.assertEqual(recover_expired(apply=True)["requeued"], 1)
        with patch("edi835.batch_jobs.timezone.now", return_value=timezone.now() + timedelta(seconds=20)):
            new = claim_job(old["id"])
        attempt = ProcessingAttempt.objects.get(pk=new["attempt_id"])
        self.assertEqual(str(attempt.parent_id), old["attempt_id"])
        self.assertEqual(attempt.sequence, 2)
        with self.assertRaises(LeaseLost):
            write_job({**old, "state": "COMPLETED"})
        with self.assertRaises(LeaseLost):
            with execution(old):
                begin_effects()
        self.assertFalse(renew(old))
        self.assertEqual(read_job(old["id"])["attempt_id"], new["attempt_id"])

    def test_expired_effects_are_not_automatically_replayed(self):
        receipt = self.register()
        job = claim_job(str(receipt.dispatch_id))
        with execution(job):
            begin_effects()
        self.expired(job)
        self.assertEqual(recover_expired(apply=True)["review_required"], 1)
        self.assertEqual(read_job(job["id"])["state"], "FAILED")
        receipt.refresh_from_db()
        self.assertEqual(receipt.safe_error_code, "RECOVERY_REVIEW_REQUIRED")

    @override_settings(INTAKE_MAX_ATTEMPTS=1)
    def test_retry_exhaustion_is_terminal(self):
        receipt = self.register()
        job = claim_job(str(receipt.dispatch_id))
        self.expired(job)
        self.assertEqual(recover_expired(apply=True)["review_required"], 1)

    def test_live_attempt_is_not_recovered_and_untokened_write_is_rejected(self):
        receipt = self.register()
        job = claim_job(str(receipt.dispatch_id))
        self.assertEqual(recover_expired(apply=True)["expired"], 0)
        with self.assertRaises(LeaseLost):
            write_job(receipt.dispatch.payload)
        self.assertEqual(read_job(job["id"])["state"], "RUNNING")

    def test_expired_owner_cannot_replace_validation_evidence(self):
        receipt = self.register()
        job = claim_job(str(receipt.dispatch_id))
        with execution(job):
            self.expired(job)
            with self.assertRaises(LeaseLost):
                record_validation(receipt, b"synthetic", outcome="VALID")
        receipt.refresh_from_db()
        self.assertEqual(receipt.validation_outcome, "PENDING")

    def test_durable_output_survives_crash_without_resending(self):
        receipt = self.register(workflow="835")
        job = claim_job(str(receipt.dispatch_id))
        output, text, source = self.output(receipt, job)
        with execution(job):
            delivery_checkpoint(output.mir_file, "SENDING")
        self.expired(job)
        with patch("edi835.services.upload_mir_to_sftp") as send:
            self.assertEqual(recover_expired(apply=True)["outputs_recovered"], 1)
            send.assert_not_called()
        output.refresh_from_db()
        self.assertEqual(output.delivery_state, "SENDING")
        self.assertEqual(read_job(job["id"])["state"], "COMPLETED")
        self.assertEqual(MIRFile.objects.count(), 1)
        token = current_transfer.set(receipt)
        try:
            with patch("edi835.services.upload_mir_to_sftp") as send:
                replayed = replay_output()
                self.assertEqual(replayed["mir_text"], text)
                self.assertEqual(replayed["db_record"], source)
                send.assert_not_called()
        finally:
            current_transfer.reset(token)

    def test_missing_output_blocks_completion_and_recovery(self):
        receipt = self.register(workflow="835")
        job = claim_job(str(receipt.dispatch_id))
        output, _, source = self.output(receipt, job)
        source_path(output.storage_key).unlink()
        with self.assertRaises(OSError):
            write_job({**job, "state": "COMPLETED", "result": {"source_id": str(source.pk)}})
        self.assertEqual(read_job(job["id"])["state"], "RUNNING")
        self.expired(job)
        self.assertEqual(recover_expired(apply=True)["review_required"], 1)

    def test_changed_output_is_not_silently_overwritten(self):
        receipt = self.register(workflow="835")
        job = claim_job(str(receipt.dispatch_id))
        output, text, source = self.output(receipt, job)
        identical = store_mir_file(source_835=source, mir_filename="synthetic.MIR", mir_text=text)
        self.assertEqual(identical.pk, output.mir_file_id)
        with self.assertRaises(ValueError):
            store_mir_file(source_835=source, mir_filename="synthetic.MIR", mir_text=text.replace(" ", "0", 1))
        verify_output(output)

    def test_hash_mismatch_in_normalized_output_is_detected(self):
        receipt = self.register(workflow="835")
        job = claim_job(str(receipt.dispatch_id))
        output, _, _ = self.output(receipt, job)
        MIRFile.objects.filter(pk=output.mir_file_id).update(file_content="damaged")
        output.refresh_from_db()
        with self.assertRaises(ValueError):
            verify_output(output)

    def test_success_without_expected_mir_checkpoint_is_rejected(self):
        receipt = self.register(workflow="835")
        job = claim_job(str(receipt.dispatch_id))
        source = EDI835File.objects.create(client=self.tenant, original_filename="x", stored_filename="x")
        with self.assertRaises(ValueError):
            write_job({**job, "state": "COMPLETED", "result": {"source_id": str(source.pk)}})

    def test_orphan_quarantine_is_explicit_reversible_and_does_not_touch_sources(self):
        receipt = self.register(asynchronous=False)
        finish_inline([receipt], True)
        root = Path(settings.INTAKE_STORAGE_ROOT)
        orphan = root / str(self.tenant.pk) / "orphan.bin"
        orphan.write_bytes(b"synthetic orphan")
        old = (timezone.now() - timedelta(days=2)).timestamp()
        os.utime(orphan, (old, old))
        self.assertEqual(len(self.command()["orphan_candidates"]), 1)
        self.assertTrue(orphan.exists())
        with self.assertRaises(CommandError):
            self.command("--apply", "--quarantine-orphans")
        report = self.command("--apply", "--quarantine-orphans", "--producers-stopped")
        self.assertEqual(report["quarantined"], 1)
        self.assertTrue(source_path(receipt.storage_key).exists())
        self.assertEqual(next(root.glob(".quarantine/*/object")).read_bytes(), b"synthetic orphan")

    def test_missing_source_fails_queued_intake_without_invoking_processor(self):
        receipt = self.register()
        source_path(receipt.storage_key).unlink()
        self.assertEqual(self.command()["invalid_sources"], [str(receipt.pk)])
        self.assertEqual(read_job(str(receipt.dispatch_id))["state"], "QUEUED")
        self.command("--apply")
        self.assertEqual(read_job(str(receipt.dispatch_id))["state"], "FAILED")

    def test_repair_only_untouched_dispatch_intent(self):
        receipt = self.register()
        BatchDispatch.objects.filter(pk=receipt.dispatch_id).update(state="FAILED")
        self.assertEqual(self.command("--apply")["unqueued_receipts"], [str(receipt.pk)])
        self.assertEqual(read_job(str(receipt.dispatch_id))["state"], "QUEUED")

    def test_validation_count_and_duplicate_are_distinct_from_dispatch_state(self):
        receipt = self.register(fixtures.SAMPLE_837.encode(), asynchronous=False)
        record_validation(receipt, fixtures.SAMPLE_837.encode(), outcome="VALID")
        finish_inline([receipt], True, outcome="DUPLICATE")
        receipt.refresh_from_db()
        self.assertEqual(receipt.transaction_set_count, 1)
        self.assertEqual(receipt.state, "DUPLICATE")
        self.assertEqual(receipt.validation_outcome, "VALID")
        self.assertEqual(read_job(str(receipt.dispatch_id))["state"], "COMPLETED")

    def test_transient_dependency_failure_before_effects_is_requeued(self):
        receipt = self.register()
        with patch("edi835.intake_worker.read_verified", side_effect=OSError("synthetic")):
            Worker()._process(receipt.dispatch.payload)
        self.assertEqual(read_job(str(receipt.dispatch_id))["state"], "QUEUED")
        self.assertEqual(ProcessingAttempt.objects.get().state, "FAILED")

    def test_inline_failure_does_not_undo_an_already_committed_success(self):
        receipt = self.register(asynchronous=False)
        finish_inline([receipt], True)
        finish_inline([receipt], False)
        receipt.refresh_from_db()
        self.assertEqual(receipt.state, "COMPLETED")

    def test_real_persistence_is_atomic_when_checkpoint_fails(self):
        from .services import _persist_checkpointed_mir
        source = EDI835File.objects.create(client=self.tenant, original_filename="x", stored_filename="x")
        header = list(" " * 334)
        header[248:252], header[332:334] = "0101", "00"
        with patch("edi835.services.checkpoint_output", side_effect=OSError("synthetic interrupted write")):
            with self.assertRaises(OSError):
                _persist_checkpointed_mir(source, "x.MIR", "".join(header), "unused", "unused")
        self.assertFalse(MIRFile.objects.exists())
        self.assertFalse(IntakeOutput.objects.exists())

    def test_conversion_replay_does_not_regenerate_or_deliver_twice(self):
        from .services import process_edi835_file_content
        header = list(" " * 334)
        header[248:252], header[332:334] = "0101", "00"
        parsed = {"text": "".join(header), "claims_count": 1, "services_count": 0, "records_count": 1,
                  "held_claims_count": 0, "delivered_claims_count": 1}
        receipt = self.register(b"synthetic", workflow="835")
        job = claim_job(str(receipt.dispatch_id))
        token = current_transfer.set(receipt)
        try:
            with execution(job), patch("edi835.services.parse_835_to_mir", return_value=parsed) as parse, patch("edi835.services.upload_mir_to_sftp", return_value=True) as send:
                begin_effects()
                first = process_edi835_file_content("synthetic", "source.835", client=self.tenant)
                self.assertTrue(first["success"], first)
                second = process_edi835_file_content("synthetic", "source.835", client=self.tenant)
                self.assertTrue(second["replayed"])
                self.assertEqual(first["db_record"].pk, second["db_record"].pk)
                parse.assert_called_once()
                send.assert_called_once()
                write_job({**job, "state": "COMPLETED", "result": {"source_id": str(first["db_record"].pk)}})
        finally:
            current_transfer.reset(token)
        # A separate submission of the same bytes is audited, never reconverted.
        from .intake_receipts import DuplicateSubmission
        with patch("edi835.services.parse_835_to_mir") as parse, patch("edi835.services.upload_mir_to_sftp") as send:
            with self.assertRaises(DuplicateSubmission):
                process_edi835_file_content("synthetic", "source.835", file_id=first["db_record"].pk, client=self.tenant)
            parse.assert_not_called()
            send.assert_not_called()
        self.assertEqual(MIRFile.objects.count(), 1)
        self.assertEqual(IntakeOutput.objects.count(), 1)

    def test_not_before_is_enforced_even_for_a_stale_queue_snapshot(self):
        receipt = self.register()
        payload = {**receipt.dispatch.payload, "not_before": (timezone.now() + timedelta(hours=1)).isoformat()}
        write_job(payload)
        self.assertIsNone(claim_job(str(receipt.dispatch_id)))
        self.assertFalse(ProcessingAttempt.objects.exists())

    def test_receipt_status_exposes_safe_metadata_without_storage_paths_or_credentials(self):
        receipt = self.register()
        Worker()._process(receipt.dispatch.payload)
        self.client.force_login(self.admin)
        response = self.client.get("/edi835/api/intake/", {"receipt_id": str(receipt.pk)})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("lifecycle_state", body)
        self.assertEqual(len(body["attempts"]), 1)
        serialized = json.dumps(body)
        self.assertNotIn("lease_owner", serialized)
        self.assertNotIn(receipt.storage_key, serialized)
        self.assertNotIn("synthetic-secret", serialized)

    def test_old_media_temporary_can_be_quarantined_without_touching_final_files(self):
        media = Path(settings.MEDIA_ROOT)
        media.mkdir(parents=True)
        temporary = media / (".synthetic.MIR." + uuid.uuid4().hex + ".tmp")
        final = media / "synthetic.MIR"
        temporary.write_bytes(b"unfinished")
        final.write_bytes(b"final")
        old = (timezone.now() - timedelta(days=2)).timestamp()
        os.utime(temporary, (old, old))
        report = self.command("--scan-media-temporaries")
        self.assertEqual(report["media_temporary_count"], 1)
        self.assertTrue(temporary.exists())
        self.command("--apply", "--quarantine-orphans", "--producers-stopped", "--scan-media-temporaries")
        self.assertFalse(temporary.exists())
        self.assertEqual(final.read_bytes(), b"final")

    def test_rejected_batch_sources_keep_their_own_outcome(self):
        from .intake_service import register_sources
        from .intake_lifecycle import record_batch_validation
        receipts, _ = register_sources(client=self.tenant, actor=self.admin, channel="STAFF", workflow="835",
            sources=[("same.835", io.BytesIO(b"valid")), ("same.835", io.BytesIO(b"invalid"))])
        token = current_transfer.set(receipts[0])
        try:
            record_batch_validation("same.835", "valid", True)
            record_batch_validation("same.835", "invalid", False)
            finish_inline(receipts, True)
        finally:
            current_transfer.reset(token)
        for receipt in receipts:
            receipt.refresh_from_db()
        self.assertEqual(receipts[0].state, "COMPLETED")
        self.assertEqual(receipts[1].state, "REJECTED")
        self.assertEqual(receipts[1].safe_error_code, "VALIDATION_REJECTED")
