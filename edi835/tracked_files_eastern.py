"""Client-facing tracked-file timestamp normalization.

The client Conversion and Archive tables consume /edi835/api/tracked-files/.
For client users, expose ISO timestamps in America/New_York so legacy table
rendering that slices the ISO string still shows the correct US Eastern wall
clock time. Staff/admin responses remain unchanged apart from conversion-hold
metadata that is needed by both portals.

Background polling can request ``?lightweight=1``. That path reads the latest
tracked-file metadata directly from the database and deliberately skips the
folder observer and per-file filesystem checks performed by the full endpoint.
Repeated callers that do not yet know about ``lightweight=1`` are also
protected: only one full filesystem-backed refresh is allowed per scope during
a short interval; intervening polls use the database-only path. This prevents
history polling from starving validation/conversion requests while retaining
periodic folder synchronization.
"""

import json
import time
from zoneinfo import ZoneInfo

from django.http import JsonResponse

from .models import EDI835File
from .views import tracked_files_list as _tracked_files_list


EASTERN = ZoneInfo("America/New_York")
FULL_REFRESH_INTERVAL_SECONDS = 15.0
_LAST_FULL_REFRESH = {}


def _to_eastern_iso(value):
    if not value:
        return value
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return value
        return parsed.astimezone(EASTERN).isoformat()
    except Exception:
        return value


def _is_truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _refresh_scope_key(request):
    user = getattr(request, "user", None)
    return (
        getattr(user, "pk", None),
        getattr(user, "client_id", None),
        bool(getattr(user, "is_staff", False)),
        str(request.GET.get("scope") or ""),
    )


def _use_lightweight_refresh(request, explicitly_lightweight=False):
    if explicitly_lightweight:
        return True
    if _is_truthy(request.GET.get("full_sync")):
        _LAST_FULL_REFRESH[_refresh_scope_key(request)] = time.monotonic()
        return False

    key = _refresh_scope_key(request)
    now = time.monotonic()
    previous = _LAST_FULL_REFRESH.get(key)
    if previous is not None and now - previous < FULL_REFRESH_INTERVAL_SECONDS:
        return True

    # Reserve the full-refresh slot before starting the potentially expensive
    # folder scan. Concurrent/repeated polls immediately take the fast path.
    _LAST_FULL_REFRESH[key] = now
    return False


def _enrich_conversion_holds(payload, include_findings=True):
    """Expose persisted claim-level hold data through the existing history API."""
    items = payload.get("files", []) if isinstance(payload, dict) else []
    ids = [item.get("id") for item in items if item.get("id")]
    if not ids:
        return payload

    fields = [
        "id",
        "delivered_claims_count",
        "held_claims_count",
    ]
    if include_findings:
        fields.append("conversion_findings")

    hold_rows = EDI835File.objects.filter(id__in=ids).values(*fields)
    by_id = {str(row["id"]): row for row in hold_rows}
    for item in items:
        row = by_id.get(str(item.get("id")))
        if not row:
            item.setdefault("delivered_claims_count", 0)
            item.setdefault("held_claims_count", 0)
            if include_findings:
                item.setdefault("conversion_findings", [])
            continue
        item["delivered_claims_count"] = row.get("delivered_claims_count") or 0
        item["held_claims_count"] = row.get("held_claims_count") or 0
        if include_findings:
            item["conversion_findings"] = row.get("conversion_findings") or []
    return payload


def _lightweight_payload(request, include_findings=False):
    """Return tracked-file metadata without folder scans or filesystem checks."""
    client = getattr(request.user, "client", None)
    deferred_fields = ["input_file_content", "mir_file__file_content"]
    if not include_findings:
        deferred_fields.append("conversion_findings")

    if request.user.is_staff:
        records = EDI835File.objects.select_related("client", "mir_file").defer(
            *deferred_fields
        )
        if request.user.is_superuser and request.GET.get("scope") == "global":
            records = records.filter(client__isnull=True)
        elif not request.user.is_superuser:
            from admin_panel.access_control import active_client_grant_ids

            records = records.filter(client_id__in=active_client_grant_ids(request.user))
        records = records.order_by("-uploaded_at")[:200]
    else:
        records = (
            EDI835File.objects.filter(client=client)
            .select_related("client", "mir_file")
            .defer(*deferred_fields)
            .order_by("-uploaded_at")[:200]
        )

    data = []
    for record in records:
        mir_record = getattr(record, "mir_file", None)
        item = {
            "id": str(record.id),
            "client_id": str(record.client_id) if record.client_id else None,
            "client_name": record.client.name if record.client else "Global System Default",
            "original_filename": record.original_filename,
            "stored_filename": record.stored_filename,
            "mir_filename": mir_record.mir_filename if mir_record and mir_record.mir_filename else "",
            "status": record.status,
            "claims_count": record.claims_count,
            "services_count": record.services_count,
            "records_count": record.records_count,
            "delivered_claims_count": record.delivered_claims_count or 0,
            "held_claims_count": record.held_claims_count or 0,
            "uploaded_at": record.uploaded_at.isoformat() if record.uploaded_at else None,
            "processing_started_at": record.processing_started_at.isoformat() if record.processing_started_at else None,
            "processing_completed_at": record.processing_completed_at.isoformat() if record.processing_completed_at else None,
            "input_path": record.input_path,
            "output_path": record.output_path,
            "archive_path": record.archive_path,
            "error_message": record.error_message,
            "validated": record.status != "ERROR",
            "processed": record.status == "ARCHIVED",
            "present_in_sftp": record.present_in_sftp,
            "present_in_archive_folder": record.present_in_archive_folder,
            "ingestion_source": record.ingestion_source or "MANUAL",
        }
        if include_findings:
            item["conversion_findings"] = record.conversion_findings or []
        data.append(item)

    return {"files": data}


def tracked_files_list_eastern(request):
    include_findings = _is_truthy(request.GET.get("include_conversion_findings", "1"))
    requested_lightweight = _is_truthy(request.GET.get("lightweight"))
    authenticated = bool(getattr(request.user, "is_authenticated", False))
    lightweight = authenticated and _use_lightweight_refresh(
        request, explicitly_lightweight=requested_lightweight
    )

    # Keep authentication and the historical endpoint contract intact. The
    # database-only fast path is used only for authenticated callers.
    if lightweight:
        payload = _lightweight_payload(request, include_findings=include_findings)
        status_code = 200
    else:
        response = _tracked_files_list(request)
        status_code = response.status_code
        try:
            payload = json.loads(response.content.decode("utf-8"))
        except Exception:
            return response
        payload = _enrich_conversion_holds(payload, include_findings=include_findings)

    # Admin/staff tables keep their existing timestamp behavior.
    if getattr(request.user, "is_staff", False):
        return JsonResponse(payload, status=status_code)

    for item in payload.get("files", []):
        for field in (
            "uploaded_at",
            "processing_started_at",
            "processing_completed_at",
        ):
            item[field] = _to_eastern_iso(item.get(field))

    return JsonResponse(payload, status=status_code)
