"""Client-facing tracked-file timestamp normalization.

The client Conversion and Archive tables consume /edi835/api/tracked-files/.
For client users, expose ISO timestamps in America/New_York so legacy table
rendering that slices the ISO string still shows the correct US Eastern wall
clock time. Staff/admin responses remain unchanged apart from conversion-hold
metadata that is needed by both portals.
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


def _enrich_conversion_holds(payload):
    """Expose persisted claim-level hold data through the existing history API."""
    items = payload.get("files", []) if isinstance(payload, dict) else []
    ids = [item.get("id") for item in items if item.get("id")]
    if not ids:
        return payload

    hold_rows = EDI835File.objects.filter(id__in=ids).values(
        "id",
        "delivered_claims_count",
        "held_claims_count",
        "conversion_findings",
    )
    by_id = {str(row["id"]): row for row in hold_rows}
    for item in items:
        row = by_id.get(str(item.get("id")))
        if not row:
            item.setdefault("delivered_claims_count", 0)
            item.setdefault("held_claims_count", 0)
            item.setdefault("conversion_findings", [])
            continue
        item["delivered_claims_count"] = row.get("delivered_claims_count") or 0
        item["held_claims_count"] = row.get("held_claims_count") or 0
        item["conversion_findings"] = row.get("conversion_findings") or []
    return payload


def tracked_files_list_eastern(request):
    response = _tracked_files_list(request)

    try:
        payload = json.loads(response.content.decode("utf-8"))
    except Exception:
        return response

    payload = _enrich_conversion_holds(payload)

    # Admin/staff tables keep their existing timestamp behavior.
    if getattr(request.user, "is_staff", False):
        return JsonResponse(payload, status=response.status_code)

    for item in payload.get("files", []):
        for field in (
            "uploaded_at",
            "processing_started_at",
            "processing_completed_at",
        ):
            item[field] = _to_eastern_iso(item.get(field))

    return JsonResponse(payload, status=response.status_code)
