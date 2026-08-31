"""Persistent daily scheduling around the existing SFTP batch/Test pipeline."""

from datetime import datetime, timedelta, timezone as dt_timezone
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .batch_jobs import active_job_for, write_job
from .models import SFTPAutomationRun, SFTPAutomationSchedule


DEFAULT_TIMEZONE = "America/New_York"


def validated_timezone(value):
    name = str(value or DEFAULT_TIMEZONE).strip()
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Select a valid IANA timezone.") from exc
    return name


def next_daily_run(run_time, timezone_name, now=None):
    """Return the next occurrence, preserving local wall time through DST."""
    zone = ZoneInfo(validated_timezone(timezone_name))
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, dt_timezone.utc)
    local_now = now.astimezone(zone)
    candidate = datetime.combine(local_now.date(), run_time, tzinfo=zone)
    if candidate <= local_now:
        candidate = datetime.combine(local_now.date() + timedelta(days=1), run_time, tzinfo=zone)
    return candidate.astimezone(dt_timezone.utc)


def _automation_actor(schedule):
    if schedule.updated_by_id:
        return schedule.updated_by
    if schedule.created_by_id:
        return schedule.created_by
    return get_user_model().objects.filter(is_staff=True, is_active=True).order_by("date_joined", "id").first()


def enqueue_due_automations(now=None):
    """Queue each due schedule once and advance it to the next local day."""
    now = now or timezone.now()
    queued = 0
    with transaction.atomic():
        due = list(
            SFTPAutomationSchedule.objects.select_for_update(skip_locked=True)
            .select_related("client", "created_by", "updated_by")
            .filter(enabled=True, next_run_at__lte=now)
            .order_by("next_run_at")[:50]
        )
        for schedule in due:
            scheduled_for = schedule.next_run_at
            schedule.last_run_at = scheduled_for
            schedule.next_run_at = next_daily_run(
                schedule.run_time, schedule.timezone, now=scheduled_for + timedelta(seconds=1)
            )
            schedule.save(update_fields=["last_run_at", "next_run_at", "updated_at"])

            run = SFTPAutomationRun.objects.create(
                schedule=schedule,
                client=schedule.client,
                scheduled_for=scheduled_for,
            )
            if schedule.client.status != "ACTIVE" or schedule.client.stage == "offboarded":
                run.status = "SKIPPED"
                run.finished_at = now
                run.error_message = "The client is inactive or offboarded; automated access is blocked."
                run.save(update_fields=["status", "finished_at", "error_message"])
                continue
            actor = _automation_actor(schedule)
            active = active_job_for(str(schedule.client_id))
            if active:
                run.status = "SKIPPED"
                run.finished_at = now
                run.error_message = "A batch conversion was already queued or running for this client."
                run.save(update_fields=["status", "finished_at", "error_message"])
                continue
            if not actor:
                run.status = "FAILED"
                run.finished_at = now
                run.error_message = "No active administrator is available to execute this schedule."
                run.save(update_fields=["status", "finished_at", "error_message"])
                continue

            job_id = str(uuid.uuid4())
            run.job_id = job_id
            run.save(update_fields=["job_id"])
            write_job({
                "id": job_id,
                "owner_user_id": str(actor.id),
                "client_id": str(schedule.client_id),
                "scope_key": str(schedule.client_id),
                "automation_run_id": str(run.id),
                "state": "QUEUED",
                "started_at": now.isoformat(),
                "worker_started_at": None,
                "finished_at": None,
                "status_code": None,
                "result": None,
            })
            queued += 1
    return queued


def mark_automation_running(job):
    run_id = job.get("automation_run_id")
    if run_id:
        SFTPAutomationRun.objects.filter(id=run_id).update(
            status="RUNNING", started_at=timezone.now()
        )


def finish_automation_run(job):
    run_id = job.get("automation_run_id")
    if not run_id:
        return
    result = job.get("result") or {}
    recon_items = result.get("sftp_837_files") or []
    recon_names = [
        item.get("filename") or (item.get("file") or {}).get("original_filename")
        for item in recon_items if isinstance(item, dict)
    ]
    mir_name = result.get("mir_filename") or ""
    success = job.get("state") == "COMPLETED" and result.get("success") is True
    errors = result.get("errors") or []
    error = result.get("error") or (errors[-1] if errors else "")
    SFTPAutomationRun.objects.filter(id=run_id).update(
        status="SUCCESS" if success else "FAILED",
        started_at=job.get("worker_started_at") or timezone.now(),
        finished_at=job.get("finished_at") or timezone.now(),
        input_835_files=[str(value) for value in (result.get("files") or [])],
        input_recon_files=[str(value) for value in recon_names if value],
        mir_output_files=[mir_name] if mir_name else [],
        processed_835_count=max(0, int(result.get("processed_count") or 0)),
        recon_file_count=len([value for value in recon_names if value]),
        error_message=str(error or ""),
        result=result,
    )


def recover_interrupted_automation_runs():
    return SFTPAutomationRun.objects.filter(status="RUNNING", finished_at__isnull=True).update(
        status="FAILED",
        finished_at=timezone.now(),
        error_message="The automation worker restarted before this run completed. Inbound files were retained for a safe retry.",
    )
