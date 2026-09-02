from datetime import datetime, time, timezone as dt_timezone
import io
import json
import stat
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, TestCase

from accounts.models import Client, User
from .models import RECONFile, SFTPAutomationRun, SFTPAutomationSchedule
from .sftp_automation import enqueue_due_automations, finish_automation_run, next_daily_run
from .views import _execute_batch_conversion
from admin_panel.email_service import send_automation_run_notice


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
        schedule.refresh_from_db()
        self.assertGreater(schedule.next_run_at, now)

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
            "worker_started_at": "2026-08-31T13:00:00+00:00", "finished_at": "2026-08-31T13:01:00+00:00",
            "result": {"success": True, "processed_count": 2, "files": ["a.835", "b.835"],
                       "mir_filename": "output.MIR", "sftp_837_files": [{"filename": "reference.837"}], "errors": []},
        })
        run.refresh_from_db()
        self.assertEqual(run.status, "SUCCESS")
        self.assertEqual(run.input_835_files, ["a.835", "b.835"])
        self.assertEqual(run.input_recon_files, ["reference.837"])
        self.assertEqual(run.mir_output_files, ["output.MIR"])
        run_email.assert_called_once()
        self.assertEqual(run_email.call_args.args[0].id, run.id)

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

            def normalize(self, path):
                return path

            def listdir_attr(self, _path):
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
        recon = RECONFile.objects.get(client=self.client_record)
        self.assertEqual(recon.import_mode, "SFTP")
        self.assertEqual(recon.status, "PROCESSED")
        self.assertEqual(recon.claim_count, 1)
