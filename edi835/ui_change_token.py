"""Tiny fingerprint for deciding whether tracked-file history must be reloaded.

The browser may ask for this token frequently.  The expensive tracked-files
payload is fetched only when this fingerprint changes.  The fingerprint is
intentionally limited to data rendered by the tracked-files response so
unrelated client/SFTP/onboarding changes do not trigger a history download.
"""

from __future__ import annotations

import hashlib
import json

from django.http import JsonResponse

from .models import EDI835File


def _client_ids_for_request(request):
    """Return None for unrestricted superuser scope, otherwise allowed client ids."""
    user = request.user
    requested = str(request.GET.get("client_id") or "").strip()

    if not getattr(user, "is_staff", False):
        client_id = getattr(user, "client_id", None)
        return [str(client_id)] if client_id else []

    if getattr(user, "is_superuser", False):
        return [requested] if requested else None

    from admin_panel.access_control import active_client_grant_ids

    allowed = {str(value) for value in active_client_grant_ids(user)}
    if requested:
        return [requested] if requested in allowed else []
    return sorted(allowed)


def _scope(qs, client_ids):
    if client_ids is None:
        return qs
    return qs.filter(client_id__in=client_ids)


def _normalize(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _tracked_rows_hash(client_ids):
    """Hash only fields that can change the tracked-files response."""
    rows = (
        _scope(EDI835File.objects.select_related("client", "mir_file"), client_ids)
        .order_by("-uploaded_at")
        .values_list(
            "id",
            "client_id",
            "client__name",
            "original_filename",
            "stored_filename",
            "status",
            "claims_count",
            "services_count",
            "records_count",
            "delivered_claims_count",
            "held_claims_count",
            "uploaded_at",
            "processing_started_at",
            "processing_completed_at",
            "input_path",
            "output_path",
            "archive_path",
            "error_message",
            "present_in_sftp",
            "present_in_archive_folder",
            "ingestion_source",
            "mir_file__mir_filename",
        )[:200]
    )

    digest = hashlib.sha256()
    for row in rows:
        normalized = [_normalize(value) for value in row]
        digest.update(
            json.dumps(normalized, separators=(",", ":"), default=str).encode("utf-8")
        )
    return digest.hexdigest()


def _held_detail_hash(client_ids):
    """Detect claim-resolution edits stored inside conversion_findings JSON."""
    rows = (
        _scope(EDI835File.objects.filter(held_claims_count__gt=0), client_ids)
        .order_by("-uploaded_at")
        .values_list("id", "held_claims_count", "conversion_findings")[:200]
    )

    digest = hashlib.sha256()
    for file_id, held_count, findings in rows:
        digest.update(str(file_id).encode("utf-8"))
        digest.update(str(held_count or 0).encode("utf-8"))
        digest.update(
            json.dumps(
                findings or [],
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    return digest.hexdigest()


def api_ui_change_token(request):
    """Return a tiny tenant-scoped token for tracked-file history changes."""
    client_ids = _client_ids_for_request(request)
    canonical = f"{_tracked_rows_hash(client_ids)}:{_held_detail_hash(client_ids)}"
    token = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    response = JsonResponse({"success": True, "token": token})
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["Pragma"] = "no-cache"
    return response
