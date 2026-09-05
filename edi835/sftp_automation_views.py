import json
import logging
from datetime import date, time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.http import JsonResponse
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from project835.decorators import authenticated_api_required, json_api_errors

from .models import SFTPAutomationRun, SFTPAutomationSchedule
from .sftp_automation import next_schedule_run, schedule_occurrences, validated_timezone
from admin_panel.email_service import send_automation_schedule_notice


logger = logging.getLogger(__name__)


def _admin_only(request):
    return bool(request.user.is_authenticated and request.user.is_staff)


def _schedule_data(schedule):
    data = {
        "id": str(schedule.id),
        "client_id": str(schedule.client_id),
        "client_name": schedule.client.name,
        "client_code": schedule.client.client_code,
        "automation_type": schedule.automation_type,
        "direction": schedule.direction,
        "run_time": schedule.run_time.strftime("%H:%M"),
        "timezone": schedule.timezone,
        "schedule_type": schedule.schedule_type,
        "interval_value": schedule.interval_value,
        "weekdays": schedule.weekdays,
        "month_days": schedule.month_days,
        "start_date": schedule.start_date.isoformat() if schedule.start_date else None,
        "end_date": schedule.end_date.isoformat() if schedule.end_date else None,
        "one_time_date": schedule.one_time_date.isoformat() if schedule.one_time_date else None,
        "misfire_policy": schedule.misfire_policy,
        "overlap_policy": schedule.overlap_policy,
        "retry_count": schedule.retry_count,
        "retry_delay_minutes": schedule.retry_delay_minutes,
        "enabled": schedule.enabled,
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        "updated_at": schedule.updated_at.isoformat(),
    }
    data["next_runs"] = [value.isoformat() for value in schedule_occurrences(schedule, count=5)] if schedule.enabled else []
    return data


def _optional_date(value, label):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Enter a valid {label}.") from exc


def _run_data(run):
    automation_type = str(run.automation_type or "835").upper()
    input_837_files = run.input_recon_files if automation_type == "837" else []
    input_recon_files = run.input_recon_files if automation_type == "RECON" else []
    input_files = (
        run.input_835_files if automation_type == "835"
        else input_837_files if automation_type == "837"
        else input_recon_files
    )
    processed_count = run.processed_835_count if automation_type == "835" else run.recon_file_count
    if run.direction == "OUTGOING":
        processed_count = len(run.sent_files or [])
    return {
        "id": str(run.id),
        "client_id": str(run.client_id),
        "client_name": run.client.name,
        "client_code": run.client.client_code,
        "automation_type": automation_type,
        "direction": run.direction,
        "automation_label": run.get_automation_type_display(),
        "scheduled_for": run.scheduled_for.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "status": run.status,
        "job_id": str(run.job_id) if run.job_id else None,
        "input_835_files": run.input_835_files,
        "input_837_files": input_837_files,
        "input_recon_files": input_recon_files,
        "input_files": input_files,
        "mir_output_files": run.mir_output_files,
        "sent_files": run.sent_files,
        "processed_835_count": run.processed_835_count,
        "recon_file_count": run.recon_file_count,
        "files_found_count": len(input_files),
        "processed_count": processed_count,
        "error_message": run.error_message,
        "result": run.result,
    }


@csrf_exempt
@json_api_errors
@authenticated_api_required
def sftp_automation(request):
    if not _admin_only(request):
        return JsonResponse({"success": False, "error": "Administrator access is required."}, status=403)

    if request.method == "GET":
        selected_client_id = (request.GET.get("client_id") or "").strip()
        schedules = SFTPAutomationSchedule.objects.select_related("client").all()
        runs = SFTPAutomationRun.objects.select_related("client")
        if selected_client_id:
            try:
                schedules = schedules.filter(client_id=selected_client_id)
                runs = runs.filter(client_id=selected_client_id)
                runs.exists()
            except (TypeError, ValueError, ValidationError):
                return JsonResponse({"success": False, "error": "Invalid client identifier."}, status=400)
        try:
            limit = min(500, max(25, int(request.GET.get("limit", "100"))))
        except ValueError:
            return JsonResponse({"success": False, "error": "Invalid run limit."}, status=400)
        return JsonResponse({
            "success": True,
            "schedules": [_schedule_data(item) for item in schedules],
            "runs": [_run_data(item) for item in runs[:limit]],
        })

    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only GET and POST are allowed."}, status=405)

    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (UnicodeDecodeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid JSON request."}, status=400)

    client_id = str(payload.get("client_id") or "").strip()
    automation_type = str(payload.get("automation_type") or "").strip().upper()
    direction = str(payload.get("direction") or ("PROCESSING" if automation_type == "835" else "INCOMING")).strip().upper()
    raw_time = str(payload.get("run_time") or "").strip()
    valid_types = {value for value, _label in SFTPAutomationSchedule.AUTOMATION_TYPES}
    allowed = {("837", "INCOMING"), ("837", "OUTGOING"), ("835", "INCOMING"),
               ("835", "PROCESSING"), ("MIR", "OUTGOING"), ("RECON", "INCOMING")}
    if not client_id or not raw_time or automation_type not in valid_types or (automation_type, direction) not in allowed:
        return JsonResponse({"success": False, "error": "Select a supported automation operation, run time, and client."}, status=400)
    try:
        hour, minute = [int(value) for value in raw_time.split(":", 1)]
        run_time = time(hour=hour, minute=minute)
        timezone_name = validated_timezone(payload.get("timezone"))
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Enter a valid time and timezone."}, status=400)

    schedule_type = str(payload.get("schedule_type") or "DAILY").strip().upper()
    if schedule_type not in {value for value, _ in SFTPAutomationSchedule.SCHEDULE_TYPES}:
        return JsonResponse({"success": False, "error": "Select a valid schedule type."}, status=400)
    try:
        interval_value = int(payload.get("interval_value") or 1)
        retry_count = int(payload.get("retry_count") or 0)
        retry_delay = int(payload.get("retry_delay_minutes") or 5)
        start_date = _optional_date(payload.get("start_date"), "start date") or timezone.now().astimezone(ZoneInfo(timezone_name)).date()
        end_date = _optional_date(payload.get("end_date"), "end date")
        one_time_date = _optional_date(payload.get("one_time_date"), "one-time date")
        weekdays = sorted({int(value) for value in (payload.get("weekdays") or [])})
        month_days = sorted({int(value) for value in (payload.get("month_days") or [])})
    except (TypeError, ValueError) as exc:
        return JsonResponse({"success": False, "error": str(exc) or "Enter valid schedule options."}, status=400)
    if not 1 <= interval_value <= 365 or not 0 <= retry_count <= 5 or not 1 <= retry_delay <= 1440:
        return JsonResponse({"success": False, "error": "Intervals must be 1–365, retries 0–5, and retry delay 1–1440 minutes."}, status=400)
    if end_date and end_date < start_date:
        return JsonResponse({"success": False, "error": "End date cannot be before the start date."}, status=400)
    if schedule_type == "ONCE" and not one_time_date:
        return JsonResponse({"success": False, "error": "Select the date for this one-time schedule."}, status=400)
    if schedule_type == "WEEKLY" and (not weekdays or any(value < 0 or value > 6 for value in weekdays)):
        return JsonResponse({"success": False, "error": "Select at least one valid weekday."}, status=400)
    if schedule_type == "MONTHLY" and (not month_days or any(value < 1 or value > 31 for value in month_days)):
        return JsonResponse({"success": False, "error": "Select at least one valid day of the month."}, status=400)
    misfire_policy = str(payload.get("misfire_policy") or "RUN_ASAP").upper()
    overlap_policy = str(payload.get("overlap_policy") or "SKIP").upper()
    if misfire_policy not in {"RUN_ASAP", "SKIP"} or overlap_policy not in {"SKIP", "QUEUE"}:
        return JsonResponse({"success": False, "error": "Select valid missed-run and overlap policies."}, status=400)

    from accounts.models import Client
    try:
        client = Client.objects.get(id=client_id)
    except (Client.DoesNotExist, ValueError, ValidationError):
        return JsonResponse({"success": False, "error": "The selected client was not found."}, status=404)

    enabled = payload.get("enabled", True) is not False
    if client.stage == "offboarded":
        return JsonResponse({
            "success": False,
            "code": "CLIENT_OFFBOARDED",
            "error": "This client has been permanently offboarded. SFTP automation schedules are locked.",
            "offboarded": True,
        }, status=409)
    if enabled and client.status != "ACTIVE":
        return JsonResponse({"success": False, "error": "Automation cannot be enabled for an inactive client."}, status=409)

    trigger = SimpleNamespace(
        timezone=timezone_name, run_time=run_time, schedule_type=schedule_type,
        interval_value=interval_value, weekdays=weekdays, month_days=month_days,
        start_date=start_date, end_date=end_date, one_time_date=one_time_date,
    )
    next_runs = schedule_occurrences(trigger, count=5)
    if enabled and not next_runs:
        return JsonResponse({"success": False, "error": "This schedule has no future run. Check its date range and selections."}, status=400)
    if payload.get("preview_only") is True:
        return JsonResponse({"success": True, "next_runs": [value.isoformat() for value in next_runs]})

    schedule, created = SFTPAutomationSchedule.objects.get_or_create(
        client=client, automation_type=automation_type, direction=direction,
        defaults={"created_by": request.user, "run_time": run_time},
    )
    schedule.run_time = run_time
    schedule.timezone = timezone_name
    schedule.schedule_type = schedule_type
    schedule.interval_value = interval_value
    schedule.weekdays = weekdays
    schedule.month_days = month_days
    schedule.start_date = start_date
    schedule.end_date = end_date
    schedule.one_time_date = one_time_date
    schedule.misfire_policy = misfire_policy
    schedule.overlap_policy = overlap_policy
    schedule.retry_count = retry_count
    schedule.retry_delay_minutes = retry_delay
    schedule.enabled = enabled
    schedule.updated_by = request.user
    schedule.next_run_at = next_schedule_run(schedule) if enabled else None
    schedule.save()
    try:
        email_sent = send_automation_schedule_notice(schedule, created=created)
    except Exception:
        logger.exception("Automation schedule saved, but its email notification failed.")
        email_sent = False
    return JsonResponse({
        "success": True,
        "created": created,
        "schedule": _schedule_data(schedule),
        "email_notification": {"sent": email_sent},
    }, status=201 if created else 200)
