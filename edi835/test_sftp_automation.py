from datetime import date, datetime, time, timezone as dt_timezone
import io
import json
import stat
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, TestCase

from accounts.models import Client, User
from .models import RECONFile, SFTPAutomationRun, SFTPAutomationSchedule
from .sftp_automation import enqueue_due_automations, finish_automation_run, next_daily_run, schedule_occurrences
from .views import _execute_batch_conversion
from admin_panel.email_service import (
    send_automation_run_notice,
    send_batch_validation_refusal_notice,
)


class SFTPAutomationTestCase(TestCase):
    def setUp(self):
        self.client_record = Client.objects.create(
            name="Automation Client", client_code="AUTO01", email="client-owner@example.com"
        )
        self.admin = User.objects.create_superuser(
            email="automation-admin@example.com", name="Automation Admin",
            mobile="1000000000", password="test-password",
        )
        self.client.force_login(self.admin)

    def test_next_daily_run_uses_schedule_timezone(self):
        now = datetime(2026, 8, 31, 16, 0, tzinfo=dt_timezone.utc)
        next_run = next_daily_run(time(13, 0), "America/New_York", now=now)
        self.assertEqual(next_run, datetime(2026, 8, 31, 17, 0, tzinfo=dt_timezone.utc))

    def test_every_two_days_uses_start_date_as_stable_anchor(self):
        trigger = SimpleNamespace(
            timezone="UTC", run_time=time(9), schedule_type="DAILY", interval_value=2,
            start_date=date(2026, 9, 1), end_date=None, one_time_date=None,
            weekdays=[], month_days=[],
        )
        runs = schedule_occurrences(trigger, 3, now=datetime(2026, 9, 2, 12, tzinfo=dt_timezone.utc))
        self.assertEqual(runs, [
            datetime(2026, 9, 3, 9, tzinfo=dt_timezone.utc),
            datetime(2026, 9, 5, 9, tzinfo=dt_timezone.utc),
            datetime(2026, 9, 7, 9, tzinfo=dt_timezone.utc),
        ])

    def test_weekly_trigger_obeys_selected_days(self):
        trigger = SimpleNamespace(
            timezone="UTC", run_time=time(10), schedule_type="WEEKLY", interval_value=1,
            start_date=date(2026, 9, 1), end_date=None, one_time_date=None,
            weekdays=[0, 2, 4], month_days=[],
        )
        runs = schedule_occurrences(trigger, 3, now=datetime(2026, 9, 1, 12, tzinfo=dt_timezone.utc))
        self.assertEqual([value.weekday() for value in runs], [2, 4, 0])

    @patch("edi835.sftp_automation_views.send_automation_schedule_notice", return_value=True)
    def test_india_standard_time_alias_is_accepted_and_canonicalized(self, _schedule_email):
        response = self.client.post(
            "/edi835/api/admin/sftp-automation/",
            data={"client_id": str(self.client_record.id), "automation_type": "837",
                  "run_time": "13:29", "timezone": "Asia/Calcutta", "enabled": True},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        schedule = SFTPAutomationSchedule.objects.get(client=self.client_record, automation_type="837")
        self.assertEqual(schedule.run_time, time(13, 29))
        self.assertEqual(schedule.timezone, "Asia/Kolkata")
        self.assertEqual(response.json()["schedule"]["timezone"], "Asia/Kolkata")

    @patch("edi835.sftp_automation_views.send_automation_schedule_notice", return_value=True)
    def test_admin_can_save_and_read_schedule(self, schedule_email):
        response = self.client.post(
            "/edi835/api/admin/sftp-automation/",
            data={"client_id": str(self.client_record.id), "automation_type": "835", "run_time": "09:30", "timezone": "America/New_York", "enabled": True},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        schedule = SFTPAutomationSchedule.objects.get(client=self.client_record)
        self.assertEqual(schedule.run_time, time(9, 30))
        self.assertIsNotNone(schedule.next_run_at)
        schedule_email.assert_called_once_with(schedule, created=True)
        self.assertTrue(response.json()["email_notification"]["sent"])
        listing = self.client.get(f"/edi835/api/admin/sftp-automation/?client_id={self.client_record.id}")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["schedules"][0]["client_code"], "AUTO01")

    def test_client_can_have_three_independent_schedules(self):
        for index, automation_type in enumerate(("835", "837", "RECON"), start=1):
            response = self.client.post(
                "/edi835/api/admin/sftp-automation/",
                data={"client_id": str(self.client_record.id), "automation_type": automation_type,
                      "run_time": f"0{index}:00", "timezone": "America/New_York", "enabled": True},
                content_type="application/json",
            )
            self.assertIn(response.status_code, (200, 201))
        self.assertEqual(SFTPAutomationSchedule.objects.filter(client=self.client_record).count(), 3)

    def test_offboarded_client_schedule_and_batch_are_locked(self):
        self.client_record.stage = "offboarded"
        self.client_record.status = "INACTIVE"
        self.client_record.save(update_fields=["stage", "status"])
        schedule = self.client.post(
            "/edi835/api/admin/sftp-automation/",
            data={"client_id": str(self.client_record.id), "automation_type": "835",
                  "run_time": "09:00", "timezone": "America/New_York", "enabled": False},
            content_type="application/json",
        )
        self.assertEqual(schedule.status_code, 409)
        self.assertEqual(schedule.json()["code"], "CLIENT_OFFBOARDED")
        batch = self.client.post(
            "/edi835/api/start-batch-conversion/",
            data={"client_id": str(self.client_record.id)}, content_type="application/json",
        )
        self.assertEqual(batch.status_code, 409)
        self.assertEqual(batch.json()["code"], "CLIENT_OFFBOARDED")

    def test_non_admin_is_forbidden(self):
        user = User.objects.create_user(
            email="client@example.com", name="Client User", mobile="2000000000",
            password="test-password", client=self.client_record,
        )
        self.client.force_login(user)
        response = self.client.get("/edi835/api/admin/sftp-automation/")
        self.assertEqual(response.status_code, 403)

    @patch("edi835.sftp_automation.write_job")
    @patch("edi835.sftp_automation.active_job_for", return_value=None)
    def test_due_schedule_queues_existing_test_pipeline(self, _active, write_job):
        now = datetime(2026, 8, 31, 16, 0, tzinfo=dt_timezone.utc)
        schedule = SFTPAutomationSchedule.objects.create(
            client=self.client_record, run_time=time(12, 0), timezone="America/New_York",
            next_run_at=now, created_by=self.admin, updated_by=self.admin,
        )
        self.assertEqual(enqueue_due_automations(now=now), 1)
        run = SFTPAutomationRun.objects.get(schedule=schedule)
        queued = write_job.call_args.args[0]
        self.assertEqual(queued["client_id"], str(self.client_record.id))
        self.assertEqual(queued["automation_run_id"], str(run.id))
        self.assertEqual(queued["automation_type"], "835")
        self.assertTrue(queued["system_automation"])
        schedule.refresh_from_db()
        self.assertGreater(schedule.next_run_at, now)

    @patch("edi835.sftp_automation.write_job")
    @patch("edi835.sftp_automation.active_job_for", return_value=None)
    def test_due_schedule_does_not_require_a_user(self, _active, write_job):
        now = datetime(2026, 8, 31, 16, 0, tzinfo=dt_timezone.utc)
        schedule = SFTPAutomationSchedule.objects.create(
            client=self.client_record, automation_type="RECON", direction="INCOMING",
            run_time=time(12), timezone="America/New_York", next_run_at=now,
        )
        self.assertEqual(enqueue_due_automations(now=now), 1)
        queued = write_job.call_args.args[0]
        self.assertEqual(queued["owner_user_id"], "")
        self.assertTrue(queued["system_automation"])
        self.assertEqual(SFTPAutomationRun.objects.get(schedule=schedule).status, "QUEUED")

    def test_history_is_paginated(self):
        schedule = SFTPAutomationSchedule.objects.create(
            client=self.client_record, run_time=time(9), timezone="UTC",
        )
        for index in range(31):
            SFTPAutomationRun.objects.create(
                schedule=schedule, client=self.client_record,
                scheduled_for=datetime(2026, 9, 1, 9, index, tzinfo=dt_timezone.utc),
            )
        response = self.client.get(
            f"/edi835/api/admin/sftp-automation/?client_id={self.client_record.id}&page=2&page_size=25"
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["runs"]), 6)
        self.assertEqual(payload["run_pagination"]["total"], 31)
        self.assertEqual(payload["run_pagination"]["total_pages"], 2)
        self.assertTrue(payload["run_pagination"]["has_previous"])
        self.assertFalse(payload["run_pagination"]["has_next"])

    @patch("edi835.sftp_automation_operations.process_multiple_edi835_files")
    def test_835_processing_result_is_safe_for_durable_json_job(self, process_files):
        from .models import EDI835File, MIRFile
        from .sftp_automation_operations import process_staged_835

        staged = EDI835File.objects.create(
            client=self.client_record,
            original_filename="input.835",
            stored_filename="input.835",
            input_file_content="ST*835*1~",
            status="UPLOADED",
        )
        generated = EDI835File.objects.create(
            client=self.client_record,
            original_filename="input.835",
            stored_filename="generated-input.835",
            input_file_content="ST*835*1~",
            status="ARCHIVED",
        )
        MIRFile.objects.create(
            source_835=generated,
            client=self.client_record,
            mir_filename="MIROUT_2026_0905.MIR",
            file_content="MIR",
            file_hash="b" * 64,
            file_size=3,
        )
        process_files.return_value = {
            "success": True,
            "db_record": generated,
            "mir_text": "MIR" * 100,
            "combined_filename": "MIROUT_2026_0905.MIR",
            "errors": [],
        }

        result = process_staged_835(self.client_record)

        json.dumps(result)
        self.assertNotIn("db_record", result)
        self.assertNotIn("mir_text", result)
        self.assertEqual(result["edi835_file_id"], str(generated.id))
        self.assertEqual(result["mir_filename"], "MIROUT_2026_0905.MIR")
        staged.refresh_from_db()
        self.assertEqual(staged.status, "ARCHIVED")

    @patch("edi835.sftp_automation.send_automation_run_notice", return_value=True)
    def test_completed_job_persists_full_run_summary(self, run_email):
        schedule = SFTPAutomationSchedule.objects.create(
            client=self.client_record, run_time=time(9), timezone="America/New_York",
            created_by=self.admin,
        )
        run = SFTPAutomationRun.objects.create(
            schedule=schedule, client=self.client_record, scheduled_for=datetime.now(dt_timezone.utc),
        )
        finish_automation_run({
            "automation_run_id": str(run.id), "state": "COMPLETED",
            "automation_type": "835",
            "worker_started_at": "2026-08-31T13:00:00+00:00", "finished_at": "2026-08-31T13:01:00+00:00",
            "result": {"success": True, "processed_count": 2, "files": ["a.835", "b.835"],
                       "mir_filename": "output.MIR", "sftp_837_files": [{"filename": "reference.837"}], "errors": []},
        })
        run.refresh_from_db()
        self.assertEqual(run.status, "SUCCESS")
        self.assertEqual(run.input_835_files, ["a.835", "b.835"])
        # An 835 run must never report a reference file, even if a malformed
        # worker result accidentally contains one.
        self.assertEqual(run.input_recon_files, [])
        self.assertEqual(run.mir_output_files, ["output.MIR"])
        run_email.assert_called_once()
        self.assertEqual(run_email.call_args.args[0].id, run.id)

    @patch("edi835.sftp_automation.send_automation_run_notice", return_value=True)
    def test_reference_run_persists_only_its_own_file_type(self, _run_email):
        run = SFTPAutomationRun.objects.create(
            client=self.client_record, automation_type="837",
            scheduled_for=datetime.now(dt_timezone.utc),
        )
        finish_automation_run({
            "automation_run_id": str(run.id), "automation_type": "837",
            "state": "COMPLETED",
            "result": {
                "success": True,
                "automation_type": "837",
                "files": ["wrong.835"],
                "sftp_837_files": [{"filename": "reference.837", "file": {"status": "PROCESSED"}}],
                "sftp_recon_files": [{"filename": "wrong-recon.csv", "file": {"status": "PROCESSED"}}],
            },
        })
        run.refresh_from_db()
        self.assertEqual(run.input_835_files, [])
        self.assertEqual(run.input_recon_files, ["reference.837"])
        self.assertEqual(run.mir_output_files, [])
        self.assertEqual(run.processed_835_count, 0)
        self.assertEqual(run.recon_file_count, 1)

        payload = self.client.get(
            f"/edi835/api/admin/sftp-automation/?client_id={self.client_record.id}"
        ).json()["runs"][0]
        self.assertEqual(payload["automation_type"], "837")
        self.assertEqual(payload["automation_label"], "837 Reference")
        self.assertEqual(payload["input_files"], ["reference.837"])
        self.assertEqual(payload["input_837_files"], ["reference.837"])
        self.assertEqual(payload["input_recon_files"], [])
        self.assertEqual(payload["files_found_count"], 1)
        self.assertEqual(payload["processed_count"], 1)

    @patch("admin_panel.email_service.send_client_email", return_value=True)
    def test_run_email_lists_every_processed_input_and_output(self, send_email):
        run = SFTPAutomationRun.objects.create(
            client=self.client_record,
            automation_type="835",
            scheduled_for=datetime.now(dt_timezone.utc),
            finished_at=datetime.now(dt_timezone.utc),
            status="SUCCESS",
            input_835_files=["first.835", "second.x12"],
            mir_output_files=["combined.MIR"],
            processed_835_count=2,
        )
        self.assertTrue(send_automation_run_notice(run))
        html = send_email.call_args.args[2]
        self.assertIn("first.835", html)
        self.assertIn("second.x12", html)
        self.assertIn("combined.MIR", html)

    @patch("admin_panel.email_service.send_client_email", return_value=True)
    def test_failed_automation_validation_email_is_not_labeled_completed(self, send_email):
        run = SFTPAutomationRun.objects.create(
            client=self.client_record,
            automation_type="835",
            scheduled_for=datetime.now(dt_timezone.utc),
            finished_at=datetime.now(dt_timezone.utc),
            status="FAILED",
            input_835_files=["invalid.835"],
            error_message="EDI validation failed: transaction is malformed",
        )
        send_automation_run_notice(run)
        subject = send_email.call_args.args[1]
        html = send_email.call_args.args[2]
        self.assertIn("Automation Validation Failed", subject)
        self.assertNotIn("Validation Completed", subject)
        self.assertIn("835 input files involved", html)
        self.assertIn("invalid.835", html)
        self.assertIn("Failure reason", html)
        self.assertIn("transaction is malformed", html)

    @patch("admin_panel.email_service.send_client_email", return_value=True)
    def test_mixed_batch_refusal_email_lists_detailed_findings_and_valid_output(self, send_email):
        request = SimpleNamespace(user=self.admin)
        sent = send_batch_validation_refusal_notice(
            self.client_record,
            request,
            refused_files=[{
                "filename": "bad.835",
                "validator_engine": "PyX12",
                "findings": [{
                    "code": "ENV-004", "segment": "ST", "element": "ST02",
                    "line": 3, "message": "ST/SE control numbers do not match.",
                    "severity": "REFUSE",
                }],
            }],
            accepted_files=["good-one.835", "good-two.835"],
            output_files=["combined.MIR"],
        )

        self.assertTrue(sent)
        subject = send_email.call_args.args[1]
        html = send_email.call_args.args[2]
        self.assertIn("1 835 file(s) refused", subject)
        for value in ("bad.835", "good-one.835", "good-two.835", "combined.MIR", "ENV-004", "ST/ST02", "line 3"):
            self.assertIn(value, html)

    @patch("edi835.views.write_job")
    @patch("edi835.views.active_job_for", return_value=None)
    def test_admin_selected_client_controls_queued_test_pipeline(self, _active, write_job):
        assigned_client = Client.objects.create(
            name="Assigned Client", client_code="ASSIGNED", email="assigned@example.com"
        )
        self.admin.client = assigned_client
        self.admin.save(update_fields=["client"])
        response = self.client.post(
            "/edi835/api/start-batch-conversion/",
            data={"client_id": str(self.client_record.id)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(write_job.call_args.args[0]["client_id"], str(self.client_record.id))

    @patch("paramiko.SSHClient")
    @patch("edi835.views.get_sftp_runtime_credentials")
    @patch("edi835.services.resolve_sftp_config")
    def test_test_pipeline_processes_and_removes_recon_as_sftp(
        self, resolve_config, runtime_credentials, ssh_client
    ):
        content = (
            "Claim ID,Member ID,Line Number,Procedure Code,Charge Amount,Paid Amount\n"
            "CLAIM-100,MEMBER-1,1,99213,100.00,80.00\n"
        ).encode("utf-8")

        class FakeSFTP:
            def __init__(self):
                self.removed = []
                self.listed = []

            def normalize(self, path):
                return path

            def listdir_attr(self, path):
                self.listed.append(path)
                return [SimpleNamespace(filename="daily-recon.csv", st_mode=stat.S_IFREG)]

            def open(self, _path, _mode):
                return io.BytesIO(content)

            def remove(self, path):
                self.removed.append(path)

            def close(self):
                pass

        fake_sftp = FakeSFTP()
        ssh_client.return_value.open_sftp.return_value = fake_sftp
        config = SimpleNamespace(
            status="CONNECTED", host="sftp.example.com", port=22,
            username="client", inbound_835_folder="/in/835",
            inbound_837_folder="/in/837", inbound_recon_folder="/in/recon",
        )
        resolve_config.return_value = config
        runtime_credentials.return_value = {
            "host": config.host, "port": 22, "username": config.username,
            "password": "secret", "ssh_key": "", "auth_method": "Password",
            "trust_unknown_key": True, "remote_folder": config.inbound_835_folder,
        }
        request = RequestFactory().post(
            "/edi835/api/start-batch-conversion/",
            data=json.dumps({"automation_type": "RECON", "client_id": str(self.client_record.id)}),
            content_type="application/json",
        )
        request.user = self.admin

        response = _execute_batch_conversion(request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["success"])
        self.assertEqual(len(payload["sftp_recon_files"]), 1)
        self.assertTrue(payload["sftp_recon_files"][0]["remote_deleted"])
        self.assertEqual(fake_sftp.removed, ["/in/recon/daily-recon.csv"])
        self.assertEqual(fake_sftp.listed, ["/in/recon"])
        recon = RECONFile.objects.get(client=self.client_record)
        self.assertEqual(recon.import_mode, "SFTP")
        self.assertEqual(recon.status, "PROCESSED")
        self.assertEqual(recon.claim_count, 1)

    def test_sftp_delete_retries_transient_failure(self):
        from .views import _remove_sftp_file_with_retry

        sftp = Mock()
        sftp.remove.side_effect = [OSError("temporary failure"), None]

        deleted, error = _remove_sftp_file_with_retry(sftp, "/in/file.835")

        self.assertTrue(deleted)
        self.assertEqual(error, "")
        self.assertEqual(sftp.remove.call_count, 2)
