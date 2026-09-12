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
        last_held_release_scan = None
        last_long_hold_alert_scan = None

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

            now = timezone.now()
            if last_held_release_scan is None or (now - last_held_release_scan).total_seconds() >= 60:
                try:
                    from edi835.held_claims import release_due_held_claims
                    result = release_due_held_claims(now=now)
                    if result.get("released"):
                        self.stdout.write(f"Released {result['released']} due held claim(s).")
                    if result.get("failed"):
                        self.stderr.write(f"{result['failed']} held claim release attempt(s) will retry.")
                except Exception as exc:
                    self.stderr.write(f"Could not release due held claims: {exc}")
                finally:
                    last_held_release_scan = now

            # Seven-day non-duplicate hold alerts change slowly, so scan once an
            # hour instead of adding a full held-file scan to every worker poll.
            if last_long_hold_alert_scan is None or (now - last_long_hold_alert_scan).total_seconds() >= 3600:
                try:
                    from edi835.long_hold_alerts import send_overdue_nonduplicate_hold_alerts
                    alert_result = send_overdue_nonduplicate_hold_alerts(now=now)
                    if alert_result.get("emailed_claims"):
                        self.stdout.write(
                            f"Emailed {alert_result['emailed_claims']} claim(s) held more than 7 days."
                        )
                    if alert_result.get("email_failures"):
                        self.stderr.write(
                            f"{alert_result['email_failures']} seven-day hold alert email(s) failed and will retry."
                        )
                except Exception as exc:
                    self.stderr.write(f"Could not send seven-day hold alerts: {exc}")
                finally:
                    last_long_hold_alert_scan = now

            pending = queued_jobs()
            if pending:
                self._process(pending[0])
                if options["once"]:
                    return
                continue
            if options["once"]:
                return
            time.sleep(max(0.25, options["poll_seconds"]))

    def _manual_conversion(self, job, user, client):
        """Run one validated manual 835 conversion outside the web request."""
        from edi835.models import EDI835File
        from edi835.progress import reset_progress_callback, set_progress_callback
        from edi835.services import process_edi835_file_content

        source = EDI835File.objects.select_related("client").filter(id=job.get("file_id")).first()
        if source is None:
            return 404, {"success": False, "error": "Validated 835 file was not found."}
        if str(source.client_id or "") != str(getattr(client, "id", "") or ""):
            return 403, {"success": False, "error": "Conversion job client does not match the source file."}

        content = (source.input_file_content or "").strip()
        if not content:
            return 409, {"success": False, "error": "Validated 835 source content is unavailable."}

        last_write = [0.0]
        last_stage = [None]

        def progress_callback(payload):
            # Updating one JSON file on every claim would add unnecessary fsync
            # overhead. Keep in-memory fields current and persist at most twice
            # per second, immediately on stage changes and at 100%.
            job.update(payload)
            now_mono = time.monotonic()
            total = int(job.get("claims_total") or 0)
            processed = int(job.get("claims_processed") or 0)
            stage = job.get("stage")
            must_write = (
                stage != last_stage[0]
                or (total > 0 and processed >= total)
                or now_mono - last_write[0] >= 0.5
            )
            if must_write:
                job["progress_updated_at"] = timezone.now().isoformat()
                write_job(job)
                last_write[0] = now_mono
                last_stage[0] = stage

        token = set_progress_callback(progress_callback)
        try:
            job.update({
                "stage": "STARTING",
                "claims_total": int(source.claims_count or 0),
                "claims_processed": 0,
                "progress_percent": 0,
                "current_claim": "",
                "progress_updated_at": timezone.now().isoformat(),
            })
            write_job(job)
            result = process_edi835_file_content(
                content,
                original_filename=source.original_filename or source.stored_filename or "file.835",
                file_id=source.id,
                client=client,
            )
        finally:
            reset_progress_callback(token)

        record = result.get("db_record") or EDI835File.objects.filter(id=source.id).first()

        if not result.get("success"):
            payload = {
                "success": False,
                "error": f"Failed to convert EDI file: {result.get('error') or 'Conversion failed.'}",
                "partial": result.get("partial", False),
                "file_id": str(record.id) if record else str(source.id),
                "output_path": getattr(record, "output_path", "") if record else "",
                "delivered_claims_count": getattr(record, "delivered_claims_count", 0) if record else 0,
                "held_claims_count": getattr(record, "held_claims_count", 0) if record else 0,
                "findings": result.get("findings", []),
            }
            return 400, payload

        mir_record = getattr(record, "mir_file", None) if record else None
        mir_filename = getattr(mir_record, "mir_filename", "") or ""
        payload = {
            "success": True,
            "text": result.get("mir_text", ""),
            "claims_count": result.get("claims_count", 0),
            "services_count": result.get("services_count", 0),
            "records_count": result.get("records_count", 0),
            "file_id": str(record.id) if record else str(source.id),
            "output_path": getattr(record, "output_path", "") if record else "",
            "archive_path": getattr(record, "archive_path", "") if record else "",
            "mir_filename": mir_filename,
            "filename": mir_filename,
            "partial": result.get("partial", False),
            "delivered_claims_count": getattr(record, "delivered_claims_count", 0) if record else 0,
            "held_claims_count": getattr(record, "held_claims_count", 0) if record else 0,
            "findings": result.get("findings", []),
        }
        return 200, payload

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
                user = SimpleNamespace(
                    id=None, name="System Automation", email="",
                    is_staff=True, is_active=True, is_authenticated=False,
                    client=None, client_id=None,
                )
            from accounts.models import Client
            client = Client.objects.get(id=job.get("client_id"))

            if job.get("job_type") == "MANUAL_CONVERSION":
                status_code, payload = self._manual_conversion(job, user, client)
                job["state"] = "COMPLETED" if payload.get("success") else "FAILED"
                job["status_code"] = status_code
                job["result"] = payload
                job["finished_at"] = timezone.now().isoformat()
                if payload.get("success"):
                    job["stage"] = "COMPLETED"
                    job["progress_percent"] = 100
                    if job.get("claims_total"):
                        job["claims_processed"] = job["claims_total"]
                else:
                    job["stage"] = "FAILED"
                job["progress_updated_at"] = job["finished_at"]
                write_job(job)
                return

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
            if job.get("job_type") == "MANUAL_CONVERSION":
                job["stage"] = "FAILED"
                job["progress_updated_at"] = timezone.now().isoformat()
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
