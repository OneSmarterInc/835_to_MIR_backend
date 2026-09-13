"""Checks API for unresolved MIR claims missing 837 and/or RECON evidence."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from django.http import JsonResponse
from django.utils import timezone

from admin_panel.access_control import scope_client_queryset

from .alert_models import ClaimAlertEmail
from .held_claims import mir_claim_number
from .missing_reference_alerts import (
    CATEGORY,
    EASTERN,
    SEND_AT,
    _837_keys,
    _claim_keys,
    _recon_keys,
    missing_reference_eligible_at,
    normalize_claim_id,
)
from .models import MIRClaim


def _alert_history_by_claim(user):
    """Return sent missing-reference alert counts/dates keyed by client + claim."""
    rows = scope_client_queryset(
        ClaimAlertEmail.objects.filter(category=CATEGORY, status="SENT"),
        user,
        field="client_id",
    ).values("client_id", "alert_date", "sent_at", "claims")

    history = defaultdict(lambda: {"count": 0, "dates": set(), "last_sent_at": None})
    for row in rows.iterator(chunk_size=500):
        client_id = str(row["client_id"])
        for claim in row.get("claims") or []:
            raw = claim.get("claim_number") or mir_claim_number(claim.get("claim_control_number") or "")
            normalized = normalize_claim_id(raw)
            if not normalized:
                continue
            key = (client_id, normalized)
            state = history[key]
            state["count"] += 1
            if row.get("alert_date"):
                state["dates"].add(row["alert_date"])
            sent_at = row.get("sent_at")
            if sent_at and (state["last_sent_at"] is None or sent_at > state["last_sent_at"]):
                state["last_sent_at"] = sent_at
    return history


def _next_email_at(*, eligible_at, now, sent_dates):
    """Return the next scheduled 5:30 PM Eastern digest slot for one claim."""
    local_now = now.astimezone(EASTERN)
    if eligible_at > local_now:
        return eligible_at, False

    today = local_now.date()
    today_slot = datetime.combine(today, SEND_AT, tzinfo=EASTERN)
    if today in sent_dates:
        next_day = today + timedelta(days=1)
        return datetime.combine(next_day, SEND_AT, tzinfo=EASTERN), False

    # If today's slot has not been sent yet, retain the 5:30 timestamp. When the
    # current time is already later than 5:30, the row is intentionally marked
    # due so operations can see that the worker should send/retry it today.
    return max(eligible_at, today_slot), local_now >= max(eligible_at, today_slot)


def missing_reference_status_rows(user, now=None):
    """Return all currently unresolved pushed MIR claims missing 837/RECON data."""
    now = now or timezone.now()
    mir_rows = scope_client_queryset(
        MIRClaim.objects.select_related(
            "mir_file",
            "mir_file__client",
            "mir_file__source_835",
        ).filter(
            mir_file__status="PUSHED",
            mir_file__client__isnull=False,
        ),
        user,
        field="mir_file__client_id",
    ).order_by("mir_file__client_id", "mir_file__updated_at", "claim_sequence")

    # Anchor the seven-day clock to the first successfully pushed MIR occurrence
    # for the client/claim, matching the email scheduler's existing semantics.
    earliest = {}
    for claim in mir_rows.iterator(chunk_size=2000):
        claim_number = mir_claim_number(claim.claim_control_number)
        normalized = normalize_claim_id(claim_number)
        if not normalized:
            continue
        client_id = str(claim.mir_file.client_id)
        key = (client_id, normalized)
        if key in earliest:
            continue
        source = claim.mir_file.source_835
        earliest[key] = {
            "client": claim.mir_file.client,
            "client_id": client_id,
            "claim_number": claim_number,
            "claim_control_number": claim.claim_control_number,
            "mir_filename": claim.mir_file.mir_filename,
            "sent_at": claim.mir_file.updated_at,
            "eligible_at": missing_reference_eligible_at(claim.mir_file.updated_at),
            "came_in_at": getattr(source, "uploaded_at", None) or claim.mir_file.updated_at,
            "source_835_filename": (
                getattr(source, "original_filename", "")
                or getattr(source, "stored_filename", "")
                or ""
            ),
        }

    alert_history = _alert_history_by_claim(user)
    reference_cache = {}
    output = []

    for key, item in earliest.items():
        client_id, normalized = key
        client = item["client"]
        if client_id not in reference_cache:
            reference_cache[client_id] = (_837_keys(client), _recon_keys(client))
        keys_837, keys_recon = reference_cache[client_id]
        identity_keys = _claim_keys(item["claim_control_number"])
        in_837 = bool(identity_keys & keys_837)
        in_recon = bool(identity_keys & keys_recon)
        if in_837 and in_recon:
            continue

        missing_in = []
        if not in_837:
            missing_in.append("837")
        if not in_recon:
            missing_in.append("RECON")

        history = alert_history.get((client_id, normalized), {"count": 0, "dates": set(), "last_sent_at": None})
        next_email_at, email_due = _next_email_at(
            eligible_at=item["eligible_at"],
            now=now,
            sent_dates=history["dates"],
        )

        output.append({
            "client_id": client_id,
            "client_name": client.name,
            "claim_number": item["claim_number"],
            "claim_control_number": str(item["claim_control_number"] or "").strip(),
            "missing_in": missing_in,
            "missing_in_label": " and ".join(missing_in),
            "came_in_at": item["came_in_at"].isoformat() if item["came_in_at"] else None,
            "source_835_filename": item["source_835_filename"],
            "mir_filename": item["mir_filename"],
            "sent_at": item["sent_at"].isoformat(),
            "eligible_at": item["eligible_at"].isoformat(),
            "next_email_at": next_email_at.isoformat(),
            "email_due": email_due,
            "email_count": int(history["count"]),
            "last_email_sent_at": history["last_sent_at"].isoformat() if history["last_sent_at"] else None,
        })

    output.sort(key=lambda row: (row["next_email_at"], row["client_name"], row["claim_number"]))
    return output


def api_missing_reference_status(request):
    rows = missing_reference_status_rows(request.user)
    return JsonResponse({
        "success": True,
        "claims": rows,
        "count": len(rows),
    })
