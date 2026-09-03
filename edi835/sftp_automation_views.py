import json
import logging
from datetime import time

from django.http import JsonResponse
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from project835.decorators import authenticated_api_required, json_api_errors

from .models import SFTPAutomationRun, SFTPAutomationSchedule
from .sftp_automation import next_daily_run, validated_timezone
from admin_panel.email_service import send_automation_schedule_notice


logger = logging.getLogger(__name__)


def _admin_only(request):
    return bool(request.user.is_authenticated and request.user.is_staff)


def _schedule_data(schedule):
    return {
        "id": str(schedule.id),
        "client_id": str(schedule.client_id),
        "client_name": schedule.client.name,
        "client_code": schedule.client.client_code,
        "automation_type": schedule.automation_type,
        "run_time": schedule.run_time.strftime("%H:%M"),
        "timezone": schedule.timezone,
        "enabled": schedule.enabled,
        "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
        "updated_at": schedule.updated_at.isoformat(),
    }


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
    return {
        "id": str(run.id),
        "client_id": str(run.client_id),
        "client_name": run.client.name,
        "client_code": run.client.client_code,
        "automation_type": automation_type,
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
    raw_time = str(payload.get("run_time") or "").strip()
    valid_types = {value for value, _label in SFTPAutomationSchedule.AUTOMATION_TYPES}
    if not client_id or not raw_time or automation_type not in valid_types:
        return JsonResponse({"success": False, "error": "Client, automation type, and run time are required."}, status=400)
    try:
        hour, minute = [int(value) for value in raw_time.split(":", 1)]
        run_time = time(hour=hour, minute=minute)
        timezone_name = validated_timezone(payload.get("timezone"))
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Enter a valid time and timezone."}, status=400)

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

    schedule, created = SFTPAutomationSchedule.objects.get_or_create(
        client=client, automation_type=automation_type,
        defaults={"created_by": request.user, "run_time": run_time},
    )
    schedule.run_time = run_time
    schedule.timezone = timezone_name
    schedule.enabled = enabled
    schedule.updated_by = request.user
    schedule.next_run_at = next_daily_run(run_time, timezone_name) if enabled else None
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
