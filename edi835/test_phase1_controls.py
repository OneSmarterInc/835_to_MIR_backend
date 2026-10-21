import copy
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from filelock import FileLock

from edi835.management.commands.run_batch_worker import Command as Worker
from edi835.management.commands.phase1_converter_demo import converter_demo
from edi835.phase1_acceptance import acceptance_gaps
from edi835.queue_observation import observe_queue


class WorkerOwnershipTests(SimpleTestCase):
    def test_lock_excludes_another_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / ".worker.lock")
            probe = (
                "import sys\nfrom filelock import FileLock, Timeout\n"
                "try:\n    FileLock(sys.argv[1], timeout=0).acquire()\n"
                "except Timeout:\n    sys.exit(0)\nsys.exit(1)\n"
            )
            with FileLock(path):
                result = subprocess.run([sys.executable, "-c", probe, path],
                                        capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_second_worker_cannot_enter_recovery_or_processing(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            queue = Path(directory) / "edi835" / "batch_jobs"
            queue.mkdir(parents=True)
            with FileLock(str(queue / ".worker.lock")), patch.object(Worker, "_run_loop") as run:
                with self.assertRaisesMessage(CommandError, "already owns"):
                    call_command("run_batch_worker", once=True)
                run.assert_not_called()

    def test_lock_released_after_failure_and_normal_exit(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            with patch.object(Worker, "_run_loop", side_effect=RuntimeError("synthetic")):
                with self.assertRaises(RuntimeError):
                    call_command("run_batch_worker", once=True)
            for _ in range(2):
                with patch.object(Worker, "_run_loop") as run:
                    run.return_value = None
                    call_command("run_batch_worker", once=True)
                    run.assert_called_once()


class QueueObservationTests(SimpleTestCase):
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    def put(self, path, name, **values):
        (path / name).write_text(json.dumps(values), encoding="utf-8")

    def test_counts_and_ages_without_payload_or_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.put(path, "private-name.json", state="QUEUED",
                     started_at=(self.now - timedelta(seconds=600)).isoformat(),
                     filename="secret.835", result={"patient": "DO NOT RETURN"})
            self.put(path, "running.json", state="RUNNING",
                     worker_started_at=(self.now - timedelta(seconds=10)).isoformat())
            before = {p.name: p.read_bytes() for p in path.iterdir()}
            result = observe_queue(path, self.now)
            self.assertEqual(result["oldest_queued_seconds"], 600)
            self.assertEqual(result["oldest_running_seconds"], 10)
            self.assertEqual(result["counts"]["QUEUED"], 1)
            self.assertTrue(result["scan_complete"])
            self.assertNotIn("secret", json.dumps(result))
            self.assertNotIn("patient", json.dumps(result))
            self.assertEqual(before, {p.name: p.read_bytes() for p in path.iterdir()})

    def test_invalid_future_naive_missing_timestamps_are_unknown(self):
        for timestamp in (None, "bad", "2026-10-20T00:00:00", "2027-01-01T00:00:00+00:00"):
            with self.subTest(timestamp=timestamp), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                self.put(path, "job.json", state="QUEUED", started_at=timestamp)
                result = observe_queue(path, self.now)
                self.assertFalse(result["scan_complete"])
                self.assertEqual(result["invalid_active_timestamps"], 1)
                self.assertIsNone(result["oldest_queued_seconds"])

    def test_scan_bounds_and_malformed_records_do_not_report_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            self.put(path, "one.json", state="COMPLETED")
            self.put(path, "two.json", state="FAILED")
            self.assertFalse(observe_queue(path, self.now, max_files=1)["scan_complete"])
            for payload in ("{}", "[]", '{"state": []}', "not json", " " * 101):
                (path / "one.json").write_text(payload)
                result = observe_queue(path, self.now, max_bytes=100)
                self.assertFalse(result["scan_complete"])
                self.assertEqual(result["unreadable_files"], 1)

    def test_missing_queue_is_not_created_and_strict_mode_fails(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            output = StringIO()
            with self.assertRaises(CommandError):
                call_command("phase1_queue_status", legacy_files=True, max_queue_age_seconds=60,
                             fail_if_unhealthy=True, stdout=output)
            self.assertEqual(json.loads(output.getvalue())["assessment"], "unknown")
            self.assertFalse((Path(directory) / "edi835").exists())

    def test_threshold_exceeded_and_empty_queue_with_explicit_threshold(self):
        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory):
            path = Path(directory) / "edi835" / "batch_jobs"
            path.mkdir(parents=True)
            output = StringIO()
            call_command("phase1_queue_status", legacy_files=True, max_queue_age_seconds=60,
                         fail_if_unhealthy=True, stdout=output)
            self.assertEqual(json.loads(output.getvalue())["assessment"], "within_queue_age_limit")
            self.put(path, "old.json", state="QUEUED", started_at="2000-01-01T00:00:00+00:00")
            with self.assertRaises(CommandError):
                call_command("phase1_queue_status", legacy_files=True, max_queue_age_seconds=60,
                             fail_if_unhealthy=True, stdout=StringIO())

    def test_strict_mode_requires_explicit_target(self):
        with self.assertRaisesMessage(CommandError, "approved"):
            call_command("phase1_queue_status", fail_if_unhealthy=True, stdout=StringIO())


class AcceptanceRecordTests(SimpleTestCase):
    def record(self):
        return json.loads((Path(settings.BASE_DIR) / "docs" / "phase1_acceptance.json").read_text())

    def complete_record(self):
        record = self.record()
        for section, status in (("approvals", "approved"), ("verification", "passed")):
            for entry in record[section].values():
                entry.update(status=status, owner_name="Synthetic reviewer", recorded_on="2026-01-01",
                             evidence_reference="Synthetic test reference")
        record["targets"] = {"max_queue_age_seconds": 60, "rpo_seconds": 0, "rto_seconds": 60}
        return record

    def test_committed_record_does_not_claim_external_approval(self):
        self.assertTrue(acceptance_gaps(self.record()))
        with self.assertRaisesMessage(CommandError, "incomplete"):
            call_command("phase1_readiness", require_approved=True, stdout=StringIO())

    def test_status_alone_cannot_approve_missing_evidence(self):
        record = self.complete_record()
        self.assertEqual(acceptance_gaps(record), [])
        for field, value in (("owner_name", None), ("evidence_reference", ""),
                             ("recorded_on", "2999-01-01")):
            changed = copy.deepcopy(record)
            changed["approvals"]["architecture"][field] = value
            self.assertTrue(acceptance_gaps(changed))
        record["targets"]["rto_seconds"] = True
        self.assertTrue(acceptance_gaps(record))

    def test_missing_sections_and_invalid_states_fail_closed(self):
        with self.assertRaises(ValueError):
            acceptance_gaps({"schema_version": 1})
        record = self.record()
        record["verification"]["restore"]["status"] = "ignore"
        with self.assertRaises(ValueError):
            acceptance_gaps(record)


class SyntheticConverterBaselineTests(SimpleTestCase):
    def test_real_parser_and_generator_have_reproducible_expected_amounts(self):
        first = converter_demo()
        self.assertTrue(first["passed"], first["checks"])
        self.assertEqual(first, converter_demo())
        output = StringIO()
        call_command("phase1_converter_demo", stdout=output)
        self.assertEqual(json.loads(output.getvalue()), first)
