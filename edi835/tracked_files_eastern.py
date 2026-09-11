"""Fast tracked-file history for client and admin portals.

Normal UI reads are database-only. They do not run folder observers or per-file
filesystem checks, so a newly created/converted/sent record can appear as soon
as it is committed to the database. A caller that genuinely needs to reconcile
folder presence can explicitly request ``?full_sync=1``.

Conversion findings remain available through ``include_conversion_findings=1``
but are loaded in one separate query only for files that actually have held
claims. This keeps ordinary history responses small as the archive grows.
"""

import json
from zoneinfo import ZoneInfo

from django.http import JsonResponse

from .models import EDI835File
from .views import tracked_files_list as _tracked_files_list


EASTERN = ZoneInfo("America/New_York")


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


def _enrich_conversion_holds(payload, include_findings=True):
    """Expose persisted claim-level hold data through the existing history API."""
    items = payload.get("files", []) if isinstance(payload, dict) else []
    ids = [item.get("id") for item in items if item.get("id")]
    if not ids:
        return payload

    fields = ["id", "delivered_claims_count", "held_claims_count"]
    if include_findings:
        fields.append("conversion_findings")

    rows = EDI835File.objects.filter(id__in=ids).values(*fields)
    by_id = {str(row["id"]): row for row in rows}
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
    """Return latest tracked-file metadata using database queries only."""
    client = getattr(request.user, "client", None)

    # Large text/JSON columns are never needed to render the history table.
    # Findings are hydrated separately below only for rows with active holds.
    deferred_fields = [
        "input_file_content",
        "conversion_findings",
        "mir_file__file_content",
    ]

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
    held_ids = []
    for record in records:
        mir_record = getattr(record, "mir_file", None)
        held_count = record.held_claims_count or 0
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
            "held_claims_count": held_count,
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
            item["conversion_findings"] = []
            if held_count:
                held_ids.append(record.id)
        data.append(item)

    if include_findings and held_ids:
        finding_rows = EDI835File.objects.filter(id__in=held_ids).values(
            "id", "conversion_findings"
        )
        findings_by_id = {
            str(row["id"]): row.get("conversion_findings") or []
            for row in finding_rows
        }
        for item in data:
            item["conversion_findings"] = findings_by_id.get(item["id"], [])

    return {"files": data}


def tracked_files_list_eastern(request):
    include_findings = _is_truthy(request.GET.get("include_conversion_findings", "1"))
    authenticated = bool(getattr(request.user, "is_authenticated", False))

    # Normal portal requests must never wait for filesystem reconciliation.
    # Full sync remains available as an explicit maintenance/reconciliation
    # operation instead of being injected into every fifteenth-second poll.
    if authenticated and not _is_truthy(request.GET.get("full_sync")):
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
