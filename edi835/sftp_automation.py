"""Persistent daily scheduling around the existing SFTP batch/Test pipeline."""

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone as dt_timezone
import logging
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .batch_jobs import active_job_for, write_job
from .models import SFTPAutomationRun, SFTPAutomationSchedule
from admin_panel.email_service import send_automation_run_notice


DEFAULT_TIMEZONE = "America/New_York"
TIMEZONE_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata",
}
logger = logging.getLogger(__name__)


def _send_run_notice_safely(run):
    try:
        return send_automation_run_notice(run)
    except Exception:
        logger.exception("Automation run %s completed, but its email notification failed.", run.id)
        return False


def validated_timezone(value):
    name = str(value or DEFAULT_TIMEZONE).strip()
    name = TIMEZONE_ALIASES.get(name, name)
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


def _local_candidate(day, run_time, zone):
    return datetime.combine(day, run_time, tzinfo=zone)


def schedule_occurrences(schedule, count=5, now=None):
    """Calculate future UTC occurrences for every supported trigger type."""
    zone = ZoneInfo(validated_timezone(schedule.timezone))
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, dt_timezone.utc)
    local_now = now.astimezone(zone)
    start = schedule.start_date or local_now.date()
    end = schedule.end_date
    schedule_type = str(schedule.schedule_type or "DAILY").upper()
    interval = max(1, int(schedule.interval_value or 1))
    results = []

    def include(day):
        if day < start or (end and day > end):
            return False
        candidate = _local_candidate(day, schedule.run_time, zone)
        if candidate > local_now:
            results.append(candidate.astimezone(dt_timezone.utc))
        return len(results) >= count

    if schedule_type == "ONCE":
        day = schedule.one_time_date
        if day and include(day):
            return results
        return results

    if schedule_type == "DAILY":
        elapsed = max(0, (local_now.date() - start).days)
        step = elapsed // interval
        day = start + timedelta(days=step * interval)
        if day < local_now.date() or _local_candidate(day, schedule.run_time, zone) <= local_now:
            day += timedelta(days=interval)
        while len(results) < count and (not end or day <= end):
            include(day)
            day += timedelta(days=interval)
        return results

    if schedule_type == "WEEKLY":
        selected = sorted({int(value) for value in (schedule.weekdays or []) if str(value).isdigit() and 0 <= int(value) <= 6})
        if not selected:
            return results
        anchor_monday = start - timedelta(days=start.weekday())
        day = max(start, local_now.date())
        for _ in range(0, 3700):
            week_index = (day - anchor_monday).days // 7
            if week_index % interval == 0 and day.weekday() in selected and include(day):
                break
            day += timedelta(days=1)
            if end and day > end:
                break
        return results

    if schedule_type == "MONTHLY":
        selected_days = sorted({int(value) for value in (schedule.month_days or []) if str(value).isdigit() and 1 <= int(value) <= 31})
        if not selected_days:
            return results
        year, month = start.year, start.month
        current_index = local_now.year * 12 + local_now.month - (year * 12 + month)
        period = max(0, current_index // interval) * interval
        for _ in range(0, 240):
            absolute = year * 12 + (month - 1) + period
            target_year, target_month = absolute // 12, absolute % 12 + 1
            last_day = monthrange(target_year, target_month)[1]
            for number in selected_days:
                if number <= last_day and include(date(target_year, target_month, number)):
                    return results
            period += interval
            if end and date(target_year, target_month, last_day) > end:
                break
        return results
    return results


def next_schedule_run(schedule, now=None):
    occurrences = schedule_occurrences(schedule, count=1, now=now)
    return occurrences[0] if occurrences else None


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
    terminal_run_ids = []
    with transaction.atomic():
        due = list(
            SFTPAutomationSchedule.objects.select_for_update(skip_locked=True)
            # Keep nullable user relations out of this locking query. PostgreSQL
            # rejects FOR UPDATE against the nullable side of an outer join.
            # Accessing created_by/updated_by below performs a normal lookup
            # while the schedule row itself remains locked by this transaction.
            .select_related("client")
            .filter(enabled=True, next_run_at__lte=now)
            .order_by("next_run_at")[:50]
        )
        for schedule in due:
            scheduled_for = schedule.next_run_at
            schedule.last_run_at = scheduled_for
            schedule.next_run_at = next_schedule_run(schedule, now=scheduled_for + timedelta(seconds=1))
            if schedule.schedule_type == "ONCE" and schedule.next_run_at is None:
                schedule.enabled = False
            schedule.save(update_fields=["last_run_at", "next_run_at", "enabled", "updated_at"])

            run = SFTPAutomationRun.objects.create(
                schedule=schedule,
                client=schedule.client,
                automation_type=schedule.automation_type,
                direction=schedule.direction,
                scheduled_for=scheduled_for,
            )
            if schedule.misfire_policy == "SKIP" and now - scheduled_for > timedelta(minutes=5):
                run.status = "SKIPPED"
                run.finished_at = now
                run.error_message = "This run was missed by more than five minutes and the schedule is configured to skip missed runs."
                run.save(update_fields=["status", "finished_at", "error_message"])
                terminal_run_ids.append(run.id)
                continue
            if schedule.client.status != "ACTIVE" or schedule.client.stage == "offboarded":
                run.status = "SKIPPED"
                run.finished_at = now
                run.error_message = "The client is inactive or offboarded; automated access is blocked."
                run.save(update_fields=["status", "finished_at", "error_message"])
                terminal_run_ids.append(run.id)
                continue
            actor = _automation_actor(schedule)
            scope_key = f"{schedule.client_id}:{schedule.automation_type}:{schedule.direction}"
            active = active_job_for(scope_key)
            if active and schedule.overlap_policy == "SKIP":
                run.status = "SKIPPED"
                run.finished_at = now
                run.error_message = "A batch conversion was already queued or running for this client."
                run.save(update_fields=["status", "finished_at", "error_message"])
                terminal_run_ids.append(run.id)
                continue
            if not actor:
                run.status = "FAILED"
                run.finished_at = now
                run.error_message = "No active administrator is available to execute this schedule."
                run.save(update_fields=["status", "finished_at", "error_message"])
                terminal_run_ids.append(run.id)
                continue

            job_id = str(uuid.uuid4())
            run.job_id = job_id
            run.save(update_fields=["job_id"])
            write_job({
                "id": job_id,
                "owner_user_id": str(actor.id),
                "client_id": str(schedule.client_id),
                "automation_type": schedule.automation_type,
                "automation_direction": schedule.direction,
                "scope_key": scope_key,
                "automation_run_id": str(run.id),
                "state": "QUEUED",
                "started_at": now.isoformat(),
                "worker_started_at": None,
                "finished_at": None,
                "status_code": None,
                "result": None,
                "attempt_count": 0,
                "retry_count": schedule.retry_count,
                "retry_delay_minutes": schedule.retry_delay_minutes,
                "not_before": None,
            })
            queued += 1
    for run in SFTPAutomationRun.objects.select_related("client", "schedule").filter(id__in=terminal_run_ids):
        _send_run_notice_safely(run)
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
    automation_type = str(job.get("automation_type") or result.get("automation_type") or "835").upper()
    direction = str(job.get("automation_direction") or result.get("direction") or "INCOMING").upper()

    def result_items(key):
        return [item for item in (result.get(key) or []) if isinstance(item, dict)]

    def result_names(key):
        return [
            item.get("filename") or (item.get("file") or {}).get("original_filename")
            for item in result_items(key)
        ]

    def imported_count(key):
        return sum(
            1 for item in result_items(key)
            if str((item.get("file") or {}).get("status") or "").upper() == "PROCESSED"
        )

    input_835_names = [str(value) for value in (result.get("files") or [])] if automation_type == "835" else []
    input_837_names = [str(value) for value in result_names("sftp_837_files") if value] if automation_type == "837" else []
    input_recon_names = [str(value) for value in result_names("sftp_recon_files") if value] if automation_type == "RECON" else []
    reference_names = input_837_names or input_recon_names
    reference_count = imported_count("sftp_837_files") if automation_type == "837" else imported_count("sftp_recon_files") if automation_type == "RECON" else 0
    mir_name = (result.get("mir_filename") or "") if automation_type == "835" else ""
    success = job.get("state") == "COMPLETED" and result.get("success") is True
    errors = result.get("errors") or []
    error = result.get("error") or (errors[-1] if errors else "")
    SFTPAutomationRun.objects.filter(id=run_id).update(
        status="SUCCESS" if success else "FAILED",
        direction=direction,
        started_at=job.get("worker_started_at") or timezone.now(),
        finished_at=job.get("finished_at") or timezone.now(),
        input_835_files=input_835_names,
        # Kept in the existing column for database compatibility. The API
        # exposes separate 837 and RECON fields based on automation_type.
        input_recon_files=reference_names,
        mir_output_files=[mir_name] if mir_name else [],
        sent_files=[str(value) for value in (result.get("sent_files") or [])],
        attempt_count=max(1, int(job.get("attempt_count") or 1)),
        processed_835_count=max(0, int(result.get("processed_count") or 0)) if automation_type == "835" else 0,
        recon_file_count=reference_count,
        error_message=str(error or ""),
        result=result,
    )
    run = SFTPAutomationRun.objects.select_related("client", "schedule").filter(id=run_id).first()
    if run:
        _send_run_notice_safely(run)


def recover_interrupted_automation_runs():
    interrupted = list(SFTPAutomationRun.objects.filter(
        status="RUNNING", finished_at__isnull=True
    ).values_list("id", flat=True))
    recovered = SFTPAutomationRun.objects.filter(id__in=interrupted).update(
        status="FAILED",
        finished_at=timezone.now(),
        error_message="The automation worker restarted before this run completed. Inbound files were retained for a safe retry.",
    )
    for run in SFTPAutomationRun.objects.select_related("client", "schedule").filter(id__in=interrupted):
        _send_run_notice_safely(run)
    return recovered
