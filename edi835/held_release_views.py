"""Read-only API for held-claim MIR release/SFTP history used by Checks."""

from __future__ import annotations

from collections import defaultdict

from django.http import JsonResponse

from .held_claims import DUPLICATE_HOLD_CODES, mir_claim_number
from .models import EDI835File


def _release_queryset(request):
    """Return held-release rows visible to the current authenticated user."""
    qs = (
        EDI835File.objects.filter(ingestion_source="HELD_RELEASE")
        .select_related("client", "mir_file")
        .prefetch_related("mir_file__claims")
    )

    if request.user.is_staff:
        if request.user.is_superuser:
            if request.GET.get("scope") == "global":
                qs = qs.filter(client__isnull=True)
        else:
            from admin_panel.access_control import active_client_grant_ids

            qs = qs.filter(client_id__in=active_client_grant_ids(request.user))
    else:
        qs = qs.filter(client=getattr(request.user, "client", None))

    return qs.order_by("-created_at")[:200]


def _provenance_maps(client_ids):
    """Index duplicate-hold findings by release MIR and claim number.

    Successful releases carry an exact ``release_mir_filename`` marker on the
    original finding. Failed attempts do not, so a latest-per-claim fallback is
    also retained to show the MIR that originally caused the duplicate hold.
    """
    exact = {}
    fallback = {}
    if not client_ids:
        return exact, fallback

    rows = (
        EDI835File.objects.filter(client_id__in=client_ids)
        .exclude(ingestion_source="HELD_RELEASE")
        .exclude(conversion_findings=[])
        .order_by("-uploaded_at")
        .values("id", "client_id", "original_filename", "conversion_findings")
    )

    for row in rows.iterator(chunk_size=100):
        for finding in row.get("conversion_findings") or []:
            if str(finding.get("rule_code") or "") not in DUPLICATE_HOLD_CODES:
                continue
            claim_number = str(finding.get("claim_number") or "").strip()
            if not claim_number:
                continue

            item = {
                "source_835_id": str(row["id"]),
                "source_835_filename": row.get("original_filename") or "",
                "held_from_mir": str(finding.get("previous_mir_filename") or ""),
                "held_from_mir_id": str(finding.get("previous_mir_id") or ""),
                "previous_sent_at": finding.get("previous_sent_at"),
                "eligible_send_at": finding.get("eligible_send_at"),
                "released_at": finding.get("released_at"),
                "release_status": str(finding.get("release_status") or ""),
                "last_release_error": str(finding.get("last_release_error") or ""),
            }
            client_id = str(row.get("client_id") or "")
            release_filename = str(finding.get("release_mir_filename") or "").strip()
            if release_filename:
                exact[(client_id, release_filename, claim_number)] = item
            fallback.setdefault((client_id, claim_number), item)

    return exact, fallback


def api_held_release_history(request):
    """List MIR files created by the held-claim release worker and their claims."""
    releases = list(_release_queryset(request))
    client_ids = {row.client_id for row in releases if row.client_id}
    exact, fallback = _provenance_maps(client_ids)

    payload = []
    for source in releases:
        mir = getattr(source, "mir_file", None)
        mir_filename = mir.mir_filename if mir else ""
        mir_status = str(mir.status if mir else "NO_MIR").upper()
        client_id = str(source.client_id or "")

        claims = []
        if mir is not None:
            for claim in mir.claims.all().order_by("claim_sequence"):
                claim_number = mir_claim_number(claim.claim_control_number)
                provenance = exact.get(
                    (client_id, mir_filename, claim_number)
                ) or fallback.get((client_id, claim_number)) or {}
                claims.append({
                    "claim_sequence": claim.claim_sequence,
                    "claim_number": claim_number,
                    "claim_control_number": claim.claim_control_number,
                    "service_count": claim.service_count,
                    "held_from_mir": provenance.get("held_from_mir", ""),
                    "held_from_mir_id": provenance.get("held_from_mir_id", ""),
                    "source_835_id": provenance.get("source_835_id", ""),
                    "source_835_filename": provenance.get("source_835_filename", ""),
                    "previous_sent_at": provenance.get("previous_sent_at"),
                    "eligible_send_at": provenance.get("eligible_send_at"),
                    "released_at": provenance.get("released_at"),
                    "release_status": provenance.get("release_status", ""),
                    "last_release_error": provenance.get("last_release_error", ""),
                })

        payload.append({
            "id": str(source.id),
            "client_id": client_id or None,
            "client_name": source.client.name if source.client else "Global System Default",
            "release_835_filename": source.original_filename,
            "mir_filename": mir_filename,
            "sftp_status": mir_status,
            "pushed": mir_status == "PUSHED",
            "present_in_sftp": bool(source.present_in_sftp),
            "claim_count": len(claims) if claims else int(source.delivered_claims_count or 0),
            "service_count": int(source.services_count or 0),
            "created_at": source.created_at.isoformat() if source.created_at else None,
            "completed_at": source.processing_completed_at.isoformat() if source.processing_completed_at else None,
            "error_message": source.error_message or "",
            "claims": claims,
        })

    return JsonResponse({"success": True, "releases": payload})
