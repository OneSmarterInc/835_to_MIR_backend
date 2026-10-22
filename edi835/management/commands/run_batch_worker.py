import json
import signal
import time
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from filelock import FileLock, Timeout

from edi835.batch_jobs import claim_job, jobs_dir, prune_finished_jobs, queued_jobs, recover_interrupted_jobs, write_job
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
        # Acquire before recovery: a second worker must never mark the first
        # worker's RUNNING jobs failed or process the same queued file.
        lock = FileLock(str(jobs_dir() / ".worker.lock"), timeout=0)
        try:
            lock.acquire()
        except Timeout as exc:
            raise CommandError("A batch worker already owns this queue directory.") from exc
        try:
            return self._run_loop(options)
        finally:
            lock.release()

    def _run_loop(self, options):
        recovered = recover_interrupted_jobs()
        if recovered:
            self.stderr.write(f"Reconciled {recovered} interrupted batch job(s).")
        pruned = prune_finished_jobs()
        if pruned:
            self.stdout.write(f"Pruned {pruned} old completed batch job file(s).")
        recovered_automations = recover_interrupted_automation_runs()
        if recovered_automations:
            self.stderr.write(f"Marked {recovered_automations} interrupted automation run(s) as failed.")
        stopping = False
        last_held_release_scan = None
        last_long_hold_alert_scan = None
        last_missing_reference_alert_scan = None
        last_queue_prune = timezone.now()
        last_lease_scan = timezone.now()

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
            if (now - last_lease_scan).total_seconds() >= 30:
                from edi835.intake_lifecycle import recover_expired
                recover_expired(apply=True)
                recover_interrupted_automation_runs()
                last_lease_scan = now
            if (now - last_queue_prune).total_seconds() >= 3600:
                try:
                    pruned = prune_finished_jobs()
                    if pruned:
                        self.stdout.write(f"Pruned {pruned} old completed batch job file(s).")
                except Exception as exc:
                    self.stderr.write(f"Could not prune completed batch job files: {exc}")
                finally:
                    last_queue_prune = now

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

            # Begin the missing-reference digest at 5:30 PM Eastern. Re-scan
            # every 15 minutes afterwards: the audit-table uniqueness guarantee
            # prevents a second successful email for the same client/day, while
            # failed SMTP sends can be retried after the same 15-minute cooldown.
            try:
                from edi835.missing_reference_alerts import EASTERN, SEND_AT, send_missing_reference_alerts
                eastern_now = now.astimezone(EASTERN)
                if (
                    eastern_now.time().replace(tzinfo=None) >= SEND_AT
                    and (
                        last_missing_reference_alert_scan is None
                        or (now - last_missing_reference_alert_scan).total_seconds() >= 900
                    )
                ):
                    missing_result = send_missing_reference_alerts(now=now)
                    if missing_result.get("emailed_claims"):
                        self.stdout.write(
                            f"Emailed {missing_result['emailed_claims']} claim(s) missing 837/RECON after 7 days."
                        )
                    if missing_result.get("email_failures"):
                        self.stderr.write(
                            f"{missing_result['email_failures']} missing 837/RECON alert email(s) failed and will retry."
                        )
                    last_missing_reference_alert_scan = now
            except Exception as exc:
                self.stderr.write(f"Could not send missing 837/RECON alerts: {exc}")
                last_missing_reference_alert_scan = now

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
        job = claim_job(job["id"])
        if job is None:
            return
        from edi835.intake_lifecycle import execution, LeaseLost
        try:
            with execution(job):
                return self._process_claimed(job)
        except LeaseLost:
            self.stderr.write("Execution lost its lease; its stale result was not committed.")

    def _process_claimed(self, job):
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
            client = Client.objects.get(id=job["client_id"]) if job.get("client_id") else None

            if job.get("job_type") == "INTAKE":
                from edi835.intake_worker import process_intake
                result = process_intake(job, user, client)
                job.update(state="COMPLETED" if result.get("success") else "FAILED",
                           intake_result=result, result=result,
                           status_code=200 if result.get("success") else 400,
                           finished_at=timezone.now().isoformat())
                write_job(job)
                return

            from edi835.intake_lifecycle import begin_effects
            begin_effects()
            if job.get("job_type") == "MANUAL_CONVERSION":
                from admin_panel.access_control import can_access_client
                authorized = can_access_client(user, client.pk) if client else getattr(user, "is_superuser", False)
                if not getattr(user, "is_active", False) or not authorized or getattr(user, "client_role", "") == "DEMO":
                    raise ValueError("Conversion authorization has expired or was revoked.")
                from edi835.intake_service import current_transfer
                from edi835.intake_lifecycle import record_validation
                from edi835.intake_storage import read_verified
                from edi835.models import InboundTransfer
                from edi835.intake_receipts import receipt_summary
                receipt = InboundTransfer.objects.filter(dispatch_id=job["id"]).first()
                token = current_transfer.set(receipt)
                try:
                    if receipt:
                        record_validation(receipt, read_verified(receipt))
                    status_code, payload = self._manual_conversion(job, user, client)
                    if receipt:
                        payload["receipts"] = [receipt_summary(receipt)]
                finally:
                    current_transfer.reset(token)
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
                if job["state"] == "FAILED" and self._safe_retry(job):
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
            from django.db import DatabaseError
            from edi835.intake_lifecycle import LeaseLost
            if isinstance(exc, LeaseLost):
                raise
            job["state"] = "FAILED"
            job["status_code"] = 500
            job["result"] = {"success": False, "error": f"Batch worker failed: {exc}"}
            if job.get("job_type") == "INTAKE":
                job["result"] = {"success": False, "error": "Intake processing failed. Review authorization, source integrity and processing records before retrying."}
                job["intake_result"] = job["result"]
                job["safe_error_code"] = "DEPENDENCY_UNAVAILABLE" if isinstance(exc, (OSError, DatabaseError)) else "INTAKE_PROCESSING_FAILED"
                job["retryable"] = isinstance(exc, (OSError, DatabaseError))
            if job.get("job_type") == "MANUAL_CONVERSION":
                job["stage"] = "FAILED"
                job["progress_updated_at"] = timezone.now().isoformat()
        if job.get("state") == "FAILED" and self._safe_retry(job):
            from datetime import timedelta
            job["state"] = "QUEUED"
            job["not_before"] = (timezone.now() + timedelta(minutes=max(1, int(job.get("retry_delay_minutes") or 5)))).isoformat()
            job["worker_started_at"] = None
            write_job(job)
            return
        job["finished_at"] = timezone.now().isoformat()
        write_job(job)
        finish_automation_run(job)

    @staticmethod
    def _safe_retry(job):
        from django.conf import settings
        from edi835.intake_lifecycle import owned_attempt
        attempt = owned_attempt(job)
        if attempt is None or attempt.effects_started_at is not None:
            return False
        if job.get("job_type") == "INTAKE":
            allowed = bool(job.get("retryable")) and attempt.sequence < int(getattr(settings, "INTAKE_MAX_ATTEMPTS", 3))
        else:
            allowed = attempt.sequence <= int(job.get("retry_count") or 0)
        if allowed:
            job["retry_reason"] = job.get("safe_error_code") or "TRANSIENT_FAILURE"
        return allowed
