"""Small change fingerprint used by the UI to avoid repeated heavy polling."""

from __future__ import annotations

import hashlib
import json

from django.db.models import Count, Max, Q, Sum
from django.http import JsonResponse

from accounts.models import Client
from admin_panel.models import ClientGoLiveStatus, ClientOffboardingStatus, ClientStepStatus

from .models import EDI835File, MIRFile, SFTPConfig


def _iso(value):
    return value.isoformat() if value else None


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


def _scope(qs, client_ids, field="client_id"):
    if client_ids is None:
        return qs
    return qs.filter(**{f"{field}__in": client_ids})


def _token_payload(request):
    client_ids = _client_ids_for_request(request)

    edi = _scope(EDI835File.objects.all(), client_ids).aggregate(
        count=Count("id"),
        latest_upload=Max("uploaded_at"),
        latest_start=Max("processing_started_at"),
        latest_complete=Max("processing_completed_at"),
        claims=Sum("claims_count"),
        records=Sum("records_count"),
        held=Sum("held_claims_count"),
        delivered=Sum("delivered_claims_count"),
        uploaded=Count("id", filter=Q(status="UPLOADED")),
        processing=Count("id", filter=Q(status="PROCESSING")),
        completed=Count("id", filter=Q(status="COMPLETED")),
        archived=Count("id", filter=Q(status="ARCHIVED")),
        errors=Count("id", filter=Q(status="ERROR")),
        in_sftp=Count("id", filter=Q(present_in_sftp=True)),
    )

    mir = _scope(MIRFile.objects.all(), client_ids).aggregate(
        count=Count("id"),
        latest_update=Max("updated_at"),
        generated=Count("id", filter=Q(status="GENERATED")),
        pushed=Count("id", filter=Q(status="PUSHED")),
        push_failed=Count("id", filter=Q(status="PUSH_FAILED")),
    )

    sftp = _scope(SFTPConfig.objects.all(), client_ids).aggregate(
        count=Count("id"),
        latest_update=Max("updated_at"),
    )

    clients = Client.objects.all()
    if client_ids is not None:
        clients = clients.filter(id__in=client_ids)
    client_state = clients.aggregate(
        count=Count("id"),
        latest_update=Max("updated_at"),
        progress=Sum("progress_pct"),
    )

    onboarding = _scope(ClientStepStatus.objects.all(), client_ids).aggregate(
        count=Count("id"), latest_update=Max("updated_at")
    )
    golive = _scope(ClientGoLiveStatus.objects.all(), client_ids).aggregate(
        count=Count("id"), latest_update=Max("updated_at")
    )
    offboarding = _scope(ClientOffboardingStatus.objects.all(), client_ids).aggregate(
        count=Count("id"), latest_update=Max("updated_at")
    )

    # Datetimes are normalized so the JSON is deterministic across requests.
    for section in (edi, mir, sftp, client_state, onboarding, golive, offboarding):
        for key, value in list(section.items()):
            if hasattr(value, "isoformat"):
                section[key] = _iso(value)
            elif value is None:
                section[key] = 0

    return {
        "edi835": edi,
        "mir": mir,
        "sftp": sftp,
        "clients": client_state,
        "onboarding": onboarding,
        "golive": golive,
        "offboarding": offboarding,
    }


def api_ui_change_token(request):
    """Return a tiny tenant-scoped fingerprint of data used by polling screens."""
    payload = _token_payload(request)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    token = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    response = JsonResponse({"success": True, "token": token})
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["Pragma"] = "no-cache"
    return response
