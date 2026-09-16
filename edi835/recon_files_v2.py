"""Fast paginated RECON archive listing and streaming downloads."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db.models import Q
from django.http import FileResponse, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from admin_panel.access_control import can_access_client, scope_client_queryset
from project835.decorators import authenticated_api_required, json_api_errors

from .models import RECONFile


def _serialize_file(item):
    return {
        "id": str(item.id),
        "client_id": str(item.client_id) if item.client_id else None,
        "client_name": item.client.name if item.client else "Global System Default",
        "client_code": item.client.client_code if item.client else "",
        "original_filename": item.original_filename,
        "stored_filename": item.stored_filename,
        "file_size": item.file_size,
        "record_count": item.record_count,
        "claim_count": item.claim_count,
        "service_count": item.service_count,
        "held_record_count": item.held_record_count,
        "parsing_findings": item.parsing_findings,
        "total_charge_amount": str(item.total_charge_amount),
        "total_paid_amount": str(item.total_paid_amount),
        "import_mode": item.import_mode or "MANUAL",
        "status": item.status,
        "processing_error": item.processing_error,
        "uploaded_by": item.uploaded_by.email if item.uploaded_by else "",
        "uploaded_at": item.uploaded_at.isoformat() if item.uploaded_at else None,
        "processing_started_at": item.processing_started_at.isoformat() if item.processing_started_at else None,
        "processed_at": item.processed_at.isoformat() if item.processed_at else None,
    }


def _base_queryset():
    return RECONFile.objects.select_related("client", "uploaded_by").defer("file_content")


def _scoped_queryset(request):
    queryset = _base_queryset()
    actor_client_id = getattr(request.user, "client_id", None)
    if actor_client_id:
        return queryset.filter(client_id=actor_client_id)
    if not request.user.is_staff:
        return queryset.none()

    client_id = str(request.GET.get("client_id") or "").strip()
    if client_id:
        if not can_access_client(request.user, client_id):
            return None
        return queryset.filter(client_id=client_id)
    if request.user.is_superuser and request.GET.get("scope") == "global":
        return queryset.filter(client__isnull=True)
    if not request.user.is_superuser:
        return scope_client_queryset(queryset, request.user)
    return queryset


def _visible_file(request, file_id):
    queryset = RECONFile.objects.select_related("client", "uploaded_by")
    actor_client_id = getattr(request.user, "client_id", None)
    if actor_client_id:
        queryset = queryset.filter(client_id=actor_client_id)
    elif not request.user.is_staff:
        return None
    elif not request.user.is_superuser:
        visible_ids = request.user.client_access_grants.filter(
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).values_list("client_id", flat=True)
        queryset = queryset.filter(client_id__in=visible_ids)
    try:
        return queryset.get(id=file_id)
    except (RECONFile.DoesNotExist, ValueError):
        return None


def _search_filter(search):
    query = (
        Q(original_filename__icontains=search)
        | Q(stored_filename__icontains=search)
        | Q(status__icontains=search)
        | Q(import_mode__icontains=search)
        | Q(client__name__icontains=search)
        | Q(client__client_code__icontains=search)
    )
    compact = search.replace(",", "").strip()
    if compact.isdigit():
        value = int(compact)
        query |= Q(claim_count=value) | Q(record_count=value) | Q(service_count=value) | Q(file_size=value)

    for date_format in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(search, date_format).date()
            query |= Q(uploaded_at__date=parsed) | Q(processed_at__date=parsed)
            break
        except ValueError:
            continue
    return query


@csrf_exempt
@authenticated_api_required
@json_api_errors
def recon_files(request):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)

    queryset = _scoped_queryset(request)
    if queryset is None:
        return JsonResponse({
            "success": False,
            "error": "Temporary approved client access is required.",
            "code": "CLIENT_GRANT_REQUIRED",
        }, status=403)

    search = str(request.GET.get("search") or "").strip()
    if search:
        queryset = queryset.filter(_search_filter(search))

    sort = str(request.GET.get("sort") or "-uploaded_at").strip()
    allowed_sorts = {
        "uploaded_at", "-uploaded_at", "original_filename", "-original_filename",
        "status", "-status", "claim_count", "-claim_count", "file_size", "-file_size",
        "import_mode", "-import_mode",
    }
    if sort not in allowed_sorts:
        sort = "-uploaded_at"
    queryset = queryset.order_by(sort, "-id")

    try:
        page = max(1, int(request.GET.get("page", "1")))
        page_size = min(100, max(10, int(request.GET.get("page_size", "25"))))
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid page parameters."}, status=400)

    total = queryset.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    rows = list(queryset[start:start + page_size])

    return JsonResponse({
        "success": True,
        "files": [_serialize_file(item) for item in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    })


@csrf_exempt
@authenticated_api_required
@json_api_errors
def recon_download(request, file_id):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)
    recon = _visible_file(request, file_id)
    if not recon:
        return JsonResponse({"success": False, "error": "RECON file was not found."}, status=404)

    safe_name = os.path.basename(recon.original_filename).replace('"', "") or "recon-file"
    archive_path = str(recon.archive_path or "").strip()
    if archive_path:
        root = Path(settings.MEDIA_ROOT).resolve()
        candidate = (root / archive_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            candidate = None
        if candidate and candidate.is_file():
            return FileResponse(candidate.open("rb"), as_attachment=True, filename=safe_name)

    content = (recon.file_content or "").encode("utf-8")
    response = HttpResponse(content, content_type="application/octet-stream")
    response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    response["Content-Length"] = len(content)
    return response
