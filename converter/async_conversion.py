"""Background manual MIR conversion endpoint.

Large 835 files must not keep a Gunicorn/nginx request open while MIR generation,
database persistence and SFTP delivery run. This endpoint queues an already
validated EDI835File for the existing durable worker and exposes short polling
requests for status/results.
"""

from __future__ import annotations

import json
import uuid

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from admin_panel.access_control import can_access_client
from edi835.batch_jobs import active_job_for, read_job, write_job
from edi835.models import EDI835File


def _can_access_record(user, record: EDI835File) -> bool:
    if record.client_id:
        return bool(can_access_client(user, record.client_id))
    return bool(getattr(user, "is_superuser", False))


def _public_job(job: dict) -> dict:
    return {
        "success": job.get("state") != "FAILED",
        "job_id": job.get("id"),
        "state": job.get("state"),
        "file_id": job.get("file_id"),
        "queued": job.get("state") in {"QUEUED", "RUNNING"},
        "priority": job.get("priority", 0 if job.get("job_type") == "MANUAL_CONVERSION" else 20),
        "stage": job.get("stage"),
        "claims_total": int(job.get("claims_total") or 0),
        "claims_processed": int(job.get("claims_processed") or 0),
        "progress_percent": float(job.get("progress_percent") or 0),
        "current_claim": job.get("current_claim") or "",
        "progress_updated_at": job.get("progress_updated_at"),
        "started_at": job.get("started_at"),
        "worker_started_at": job.get("worker_started_at"),
        "finished_at": job.get("finished_at"),
        "status_code": job.get("status_code"),
        "result": job.get("result") if job.get("state") in {"COMPLETED", "FAILED"} else None,
    }


@csrf_exempt
def api_convert_async(request):
    user = getattr(request, "user", None)

    if request.method == "GET":
        job_id = request.GET.get("job_id")
        if not job_id:
            return JsonResponse({"success": False, "error": "job_id is required."}, status=400)
        job = read_job(job_id)
        if not job or job.get("job_type") != "MANUAL_CONVERSION":
            return JsonResponse({"success": False, "error": "Conversion job was not found."}, status=404)

        record = EDI835File.objects.filter(id=job.get("file_id")).only("id", "client_id").first()
        if record is None or not _can_access_record(user, record):
            return JsonResponse({"success": False, "error": "Access denied."}, status=403)
        return JsonResponse(_public_job(job))

    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only GET and POST are allowed."}, status=405)

    try:
        body = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JsonResponse({"success": False, "error": "Invalid JSON request."}, status=400)

    file_id = str(body.get("file_id") or "").strip()
    if not file_id:
        return JsonResponse({
            "success": False,
            "error": "A validated file_id is required for background conversion.",
        }, status=400)

    try:
        record = EDI835File.objects.select_related("client").get(id=file_id)
    except (EDI835File.DoesNotExist, ValueError):
        return JsonResponse({"success": False, "error": "Validated 835 file was not found."}, status=404)

    if not _can_access_record(user, record):
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)

    if record.client and str(record.client.stage or "").lower() == "offboarded":
        return JsonResponse({
            "success": False,
            "code": "CLIENT_OFFBOARDED",
            "error": "This client has been permanently offboarded. New file processing is locked.",
        }, status=409)

    if not (record.input_file_content or "").strip():
        return JsonResponse({
            "success": False,
            "error": "The validated 835 source content is unavailable for conversion.",
        }, status=409)

    scope_key = f"manual-convert:{record.id}"
    existing = active_job_for(scope_key)
    if existing:
        payload = _public_job(existing)
        payload["message"] = "This file is already queued or processing."
        return JsonResponse(payload, status=202)

    now = timezone.now()
    job = {
        "id": str(uuid.uuid4()),
        "job_type": "MANUAL_CONVERSION",
        "priority": 0,
        "scope_key": scope_key,
        "state": "QUEUED",
        "stage": "QUEUED",
        "file_id": str(record.id),
        "client_id": str(record.client_id or ""),
        "owner_user_id": str(getattr(user, "id", "") or ""),
        "claims_total": int(record.claims_count or 0),
        "claims_processed": 0,
        "progress_percent": 0,
        "current_claim": "",
        "progress_updated_at": now.isoformat(),
        "started_at": now.isoformat(),
        "worker_started_at": None,
        "finished_at": None,
        "status_code": None,
        "result": None,
        "attempt_count": 0,
        "retry_count": 0,
    }
    write_job(job)

    if record.status != "PROCESSING":
        record.status = "PROCESSING"
        record.processing_started_at = now
        record.processing_completed_at = None
        record.error_message = ""
        record.save(update_fields=[
            "status", "processing_started_at", "processing_completed_at", "error_message"
        ])
    elif not record.processing_started_at:
        record.processing_started_at = now
        record.save(update_fields=["processing_started_at"])

    payload = _public_job(job)
    payload["message"] = "MIR conversion queued at highest priority for background processing."
    return JsonResponse(payload, status=202)
