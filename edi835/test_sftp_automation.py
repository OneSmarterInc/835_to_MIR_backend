from datetime import datetime, time, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase

from accounts.models import Client, User
from .models import SFTPAutomationRun, SFTPAutomationSchedule
from .sftp_automation import enqueue_due_automations, finish_automation_run, next_daily_run


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

    def test_admin_can_save_and_read_schedule(self):
        response = self.client.post(
            "/edi835/api/admin/sftp-automation/",
            data={"client_id": str(self.client_record.id), "automation_type": "835", "run_time": "09:30", "timezone": "America/New_York", "enabled": True},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        schedule = SFTPAutomationSchedule.objects.get(client=self.client_record)
        self.assertEqual(schedule.run_time, time(9, 30))
        self.assertIsNotNone(schedule.next_run_at)
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

    def test_completed_job_persists_full_run_summary(self):
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
