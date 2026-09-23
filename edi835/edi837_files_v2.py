"""837 file-list API with accurate inbound source and saved naming format."""

import json
import os
import uuid

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone

from project835.decorators import authenticated_api_required, json_api_errors

from .batch_jobs import active_job_for, write_job
from .edi837_views import _client_for_request
from .edi837_naming_views import get_saved_837_filename_format
from .models import EDI837File


def _is_sftp_inbound(item):
    if str(item.import_mode or "").upper() == "SFTP":
        return True
    return bool(str(item.remote_path or "").strip())


def _confirmed_remote_outbound(item):
    """Only an absolute remote SFTP path counts as a successful push.

    ingest_837 also creates a local 837_out working copy and stores that local
    relative path in outbound_path. That local copy must never be displayed as
    a successful SFTP delivery.
    """
    outbound = str(item.outbound_path or "").strip()
    return outbound if outbound.startswith("/") else ""


def _inbound_filename(item, sftp_inbound):
    remote = str(item.remote_path or "").strip()
    if sftp_inbound and remote:
        name = os.path.basename(remote.rstrip("/"))
        if name:
            return name
    return item.original_filename


def _pending_outbound_files(client):
    """Processed 837 files whose outbound_path is still local/not confirmed remotely."""
    return EDI837File.objects.filter(client=client, status="PROCESSED").exclude(
        outbound_path__startswith="/"
    )


def _queue_pending_outbound(request, client):
    if str(client.stage or "").lower() == "offboarded":
        return JsonResponse({
            "success": False,
            "code": "CLIENT_OFFBOARDED",
            "offboarded": True,
            "error": "This client has been permanently offboarded. 837 SFTP delivery is locked.",
        }, status=409)

    pending_count = _pending_outbound_files(client).count()
    filename_format = get_saved_837_filename_format(client)
    if pending_count == 0:
        return JsonResponse({
            "success": True,
            "state": "COMPLETED",
            "pending_count": 0,
            "filename_format": filename_format,
            "message": "All processed 837 files have already been pushed to SFTP.",
        })

    scope_key = f"{client.id}:837:OUTGOING"
    existing = active_job_for(scope_key)
    if existing:
        return JsonResponse({
            "success": True,
            "job_id": existing["id"],
            "state": existing["state"],
            "pending_count": pending_count,
            "filename_format": filename_format,
            "message": "The remaining 837 SFTP push is already queued or running.",
        }, status=202)

    job_id = str(uuid.uuid4())
    write_job({
        "id": job_id,
        "owner_user_id": str(request.user.id),
        "client_id": str(client.id),
        "automation_type": "837",
        "automation_direction": "OUTGOING",
        "scope_key": scope_key,
        "state": "QUEUED",
        "started_at": timezone.now().isoformat(),
        "worker_started_at": None,
        "finished_at": None,
        "status_code": None,
        "result": None,
        "attempt_count": 0,
        "retry_count": 0,
        "retry_delay_minutes": 5,
        "not_before": None,
    })
    return JsonResponse({
        "success": True,
        "job_id": job_id,
        "state": "QUEUED",
        "pending_count": pending_count,
        "filename_format": filename_format,
        "message": f"Queued {pending_count} remaining 837 file(s) for sequential SFTP delivery.",
    }, status=202)


# 2026-09-23 - Yash: Removed csrf_exempt decorator for CSRF protection
@authenticated_api_required
@json_api_errors
def edi837_files(request):
    """List 837 files or queue pending outbound delivery.

    POST is intentionally CSRF-exempt because this endpoint is an authenticated
    JSON API used by both token-backed admin sessions and normal portal sessions.
    Authentication and tenant authorization continue to be enforced by the
    existing API decorators and ``_client_for_request``.
    """
    if request.method not in {"GET", "POST"}:
        return JsonResponse({"success": False, "error": "Only GET and POST are allowed."}, status=405)

    if request.method == "POST":
        try:
            body = json.loads(request.body.decode("utf-8")) if request.body else {}
        except (TypeError, ValueError, UnicodeDecodeError):
            return JsonResponse({"success": False, "error": "Invalid JSON request."}, status=400)
        requested_client_id = body.get("client_id") or body.get("client")
    else:
        requested_client_id = request.GET.get("client_id")

    client = _client_for_request(request, requested_client_id)
    if client is None:
        return JsonResponse({"success": False, "error": "Select an authorized client."}, status=400)

    if request.method == "POST":
        return _queue_pending_outbound(request, client)

    query = str(request.GET.get("q") or "").strip()
    files = EDI837File.objects.filter(client=client)

    if query:
        status_query = query.upper().replace(" ", "_")
        source_query = query.upper()
        filters = (
            Q(original_filename__icontains=query)
            | Q(stored_filename__icontains=query)
            | Q(remote_path__icontains=query)
            | Q(outbound_path__icontains=query)
        )
        if status_query in {choice[0] for choice in EDI837File.STATUS_CHOICES}:
            filters |= Q(status=status_query)
        if source_query in {choice[0] for choice in EDI837File.IMPORT_MODE_CHOICES}:
            filters |= Q(import_mode=source_query)
        if source_query == "SFTP":
            filters |= Q(remote_path__startswith="/")
        files = files.filter(filters)
        if status_query == "NOT_PUSHED":
            files = files.exclude(outbound_path__startswith="/")
        elif status_query == "PUSHED":
            files = files.filter(outbound_path__startswith="/")

    try:
        page_number = max(1, int(request.GET.get("page", "1")))
        page_size = min(100, max(10, int(request.GET.get("page_size", "20"))))
    except ValueError:
        page_number, page_size = 1, 20

    paginator = Paginator(files.order_by("-uploaded_at"), page_size)
    page = paginator.get_page(page_number)

    rows = []
    stale_sftp_ids = []
    for item in page.object_list:
        sftp_inbound = _is_sftp_inbound(item)
        if sftp_inbound and item.import_mode != "SFTP":
            stale_sftp_ids.append(item.id)

        remote_outbound = _confirmed_remote_outbound(item)
        inbound_name = _inbound_filename(item, sftp_inbound)
        outbound_name = os.path.basename(remote_outbound.rstrip("/")) if remote_outbound else ""

        rows.append({
            "id": str(item.id),
            "file_name": inbound_name,
            "original_file_name": inbound_name,
            "stored_file_name": item.stored_filename,
            "outbound_file_name": outbound_name,
            "inbound_path": str(item.remote_path or ""),
            "outbound_path": remote_outbound,
            "status": item.status,
            "inbound_source": "SFTP" if sftp_inbound else item.get_import_mode_display(),
            "inbound_status": "Received",
            "outbound_status": "Pushed" if remote_outbound else "Not pushed",
            "outbound_ready": bool(remote_outbound),
            "claim_count": item.claim_count,
            "service_count": item.service_count,
            "total_charge_amount": str(item.total_charge_amount),
            "uploaded_at": item.uploaded_at.isoformat(),
            "processed_at": item.processed_at.isoformat() if item.processed_at else None,
        })

    if stale_sftp_ids:
        EDI837File.objects.filter(id__in=stale_sftp_ids).update(import_mode="SFTP")

    return JsonResponse({
        "success": True,
        "filename_format": get_saved_837_filename_format(client),
        "pending_outbound_count": _pending_outbound_files(client).count(),
        "results": rows,
        "count": paginator.count,
        "page": page.number,
        "page_size": page_size,
        "pages": paginator.num_pages,
        "has_previous": page.has_previous(),
        "has_next": page.has_next(),
    })
