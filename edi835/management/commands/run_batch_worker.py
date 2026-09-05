import json
import signal
import time
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from edi835.batch_jobs import queued_jobs, recover_interrupted_jobs, write_job
from edi835.sftp_automation import (
    enqueue_due_automations, finish_automation_run, mark_automation_running,
    recover_interrupted_automation_runs,
)


class Command(BaseCommand):
    help = "Run the isolated 835 batch conversion worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one queued job and exit.")
        parser.add_argument("--poll-seconds", type=float, default=2.0)

    def handle(self, *args, **options):
        recovered = recover_interrupted_jobs()
        if recovered:
            self.stderr.write(f"Marked {recovered} interrupted batch job(s) as failed.")
        recovered_automations = recover_interrupted_automation_runs()
        if recovered_automations:
            self.stderr.write(f"Marked {recovered_automations} interrupted automation run(s) as failed.")
        stopping = False

        def stop(*_args):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not stopping:
            try:
                enqueue_due_automations()
            except Exception as exc:
                self.stderr.write(f"Could not enqueue due SFTP automations: {exc}")
                time.sleep(max(0.25, options["poll_seconds"]))
                continue
            pending = queued_jobs()
            if pending:
                self._process(pending[0])
                if options["once"]:
                    return
                continue
            if options["once"]:
                return
            time.sleep(max(0.25, options["poll_seconds"]))

    def _process(self, job):
        job["state"] = "RUNNING"
        job["attempt_count"] = int(job.get("attempt_count") or 0) + 1
        job["worker_started_at"] = timezone.now().isoformat()
        write_job(job)
        mark_automation_running(job)
        try:
            user = None
            owner_user_id = job.get("owner_user_id")
            if owner_user_id:
                user = get_user_model().objects.filter(id=owner_user_id).first()
            if user is None and not job.get("system_automation"):
                raise ValueError("The user who started this batch job no longer exists.")
            if user is None:
                # Server schedules must run without a signed-in user.  This
                # principal authorizes tenant selection but is deliberately
                # unauthenticated so it is never persisted as a human actor.
                user = SimpleNamespace(
                    id=None, name="System Automation", email="",
                    is_staff=True, is_active=True, is_authenticated=False,
                    client=None, client_id=None,
                )
            from accounts.models import Client
            client = Client.objects.get(id=job.get("client_id"))

            # Scheduled directional automations explicitly carry an
            # automation_direction. Manual Conversion -> Test jobs do not;
            # they use automation_type=ALL and must continue through the
            # existing full batch pipeline below. Previously the worker
            # invented INCOMING for those ALL jobs and sent (ALL, INCOMING)
            # to the directional dispatcher, which raises
            # "Unsupported SFTP automation operation."
            direction = job.get("automation_direction")
            directional = None
            if direction:
                from edi835.sftp_automation_operations import execute_directional_operation
                directional = execute_directional_operation(
                    client,
                    user,
                    job.get("automation_type") or "835",
                    direction,
                )

            if directional is not None:
                job["state"] = "COMPLETED" if directional.get("success") else "FAILED"
                job["status_code"] = 200 if directional.get("success") else 400
                job["result"] = directional
                if job["state"] == "FAILED" and job["attempt_count"] <= int(job.get("retry_count") or 0):
                    from datetime import timedelta
                    job["state"] = "QUEUED"
                    job["not_before"] = (timezone.now() + timedelta(minutes=max(1, int(job.get("retry_delay_minutes") or 5)))).isoformat()
                    job["worker_started_at"] = None
                    write_job(job)
                    return
                job["finished_at"] = timezone.now().isoformat()
                write_job(job)
                finish_automation_run(job)
                return

            body = json.dumps({
                "client_id": job.get("client_id") or "",
                "automation_type": job.get("automation_type") or "ALL",
            }).encode("utf-8")
            request_context = SimpleNamespace(method="POST", body=body, user=user)
            # Import after Django has initialized and after the job is claimed.
            from edi835.views import _execute_batch_conversion
            response = _execute_batch_conversion(request_context)
            payload = json.loads(response.content.decode("utf-8"))
            job["state"] = "COMPLETED" if payload.get("success") else "FAILED"
            job["status_code"] = response.status_code
            job["result"] = payload
        except Exception as exc:
            job["state"] = "FAILED"
            job["status_code"] = 500
            job["result"] = {"success": False, "error": f"Batch worker failed: {exc}"}
        if job.get("state") == "FAILED" and job["attempt_count"] <= int(job.get("retry_count") or 0):
            from datetime import timedelta
            job["state"] = "QUEUED"
            job["not_before"] = (timezone.now() + timedelta(minutes=max(1, int(job.get("retry_delay_minutes") or 5)))).isoformat()
            job["worker_started_at"] = None
            write_job(job)
            return
        job["finished_at"] = timezone.now().isoformat()
        write_job(job)
        finish_automation_run(job)
