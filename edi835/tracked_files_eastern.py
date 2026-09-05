"""Client-facing tracked-file timestamp normalization.

The client Conversion and Archive tables consume /edi835/api/tracked-files/.
For client users, expose ISO timestamps in America/New_York so legacy table
rendering that slices the ISO string still shows the correct US Eastern wall
clock time. Staff/admin responses remain unchanged.
"""

import json
from zoneinfo import ZoneInfo

from django.http import JsonResponse

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


def tracked_files_list_eastern(request):
    response = _tracked_files_list(request)

    # Admin/staff tables keep their existing behavior. This change is scoped
    # specifically to the client portal.
    if getattr(request.user, "is_staff", False):
        return response

    try:
        payload = json.loads(response.content.decode("utf-8"))
    except Exception:
        return response

    for item in payload.get("files", []):
        for field in (
            "uploaded_at",
            "processing_started_at",
            "processing_completed_at",
        ):
            item[field] = _to_eastern_iso(item.get(field))

    return JsonResponse(payload, status=response.status_code)
