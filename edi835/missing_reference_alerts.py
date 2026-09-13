"""Daily 5:30 PM Eastern alerts for pushed MIR claims missing 837/RECON evidence."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone as dt_timezone
from html import escape
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone

from admin_panel.email_service import send_client_email

from .alert_history import alert_recipients, mark_alert_failed, mark_alert_sent, reserve_daily_alert
from .held_claims import mir_claim_number
from .models import EDI837Claim, MIRClaim, RECONClaim


EASTERN = ZoneInfo("America/New_York")
MISSING_REFERENCE_AGE_DAYS = 7
SEND_AT = time(hour=17, minute=30)
CATEGORY = "MISSING_REFERENCE"


def normalize_claim_id(value) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def missing_reference_eligible_at(sent_at):
    """Return 5:30 PM Eastern seven calendar-day offsets after a successful MIR send."""
    if sent_at is None:
        return None
    if timezone.is_naive(sent_at):
        sent_at = timezone.make_aware(sent_at, dt_timezone.utc)
    local = sent_at.astimezone(EASTERN)
    eligible_date = local.date() + timedelta(days=MISSING_REFERENCE_AGE_DAYS)
    return datetime.combine(eligible_date, SEND_AT, tzinfo=EASTERN)


def _claim_keys(value) -> set[str]:
    return {
        key
        for key in (normalize_claim_id(mir_claim_number(value)), normalize_claim_id(value))
        if key
    }


def _837_keys(client) -> set[str]:
    keys = set()
    rows = EDI837Claim.objects.filter(client=client, edi_file__status="PROCESSED").values_list(
        "highmark_claim_number", "claim_control_number"
    )
    for highmark, control in rows.iterator(chunk_size=2000):
        for value in (highmark, control):
            normalized = normalize_claim_id(value)
            if normalized:
                keys.add(normalized)
    return keys


def _recon_keys(client) -> set[str]:
    keys = set()
    rows = RECONClaim.objects.filter(
        Q(client=client) | Q(client__isnull=True, recon_file__client=client),
        recon_file__status__in=("PROCESSED", "PARTIAL"),
    ).values_list("claim_control_number", flat=True)
    for value in rows.iterator(chunk_size=2000):
        normalized = normalize_claim_id(value)
        if normalized:
            keys.add(normalized)
    return keys


def _collect_due_missing(now):
    """Return one unresolved row per client/claim, anchored to its first pushed MIR send."""
    local_now = now.astimezone(EASTERN)
    if local_now.time().replace(tzinfo=None) < SEND_AT:
        return {}

    latest_due_date = local_now.date() - timedelta(days=MISSING_REFERENCE_AGE_DAYS)
    next_date = latest_due_date + timedelta(days=1)
    query_end = datetime(next_date.year, next_date.month, next_date.day, tzinfo=EASTERN).astimezone(dt_timezone.utc)

    rows = (
        MIRClaim.objects.select_related("mir_file", "mir_file__client")
        .filter(
            mir_file__status="PUSHED",
            mir_file__client__isnull=False,
            mir_file__updated_at__lt=query_end,
        )
        .order_by("mir_file__client_id", "mir_file__updated_at", "claim_sequence")
    )

    earliest = {}
    for claim in rows.iterator(chunk_size=2000):
        claim_number = mir_claim_number(claim.claim_control_number)
        normalized_short = normalize_claim_id(claim_number)
        if not normalized_short:
            continue
        key = (str(claim.mir_file.client_id), normalized_short)
        sent_at = claim.mir_file.updated_at
        eligible_at = missing_reference_eligible_at(sent_at)
        if eligible_at > local_now:
            continue
        if key not in earliest:
            earliest[key] = {
                "client": claim.mir_file.client,
                "claim_number": claim_number,
                "claim_control_number": claim.claim_control_number,
                "mir_filename": claim.mir_file.mir_filename,
                "sent_at": sent_at,
                "eligible_at": eligible_at,
            }

    by_client = defaultdict(list)
    client_cache = {}
    for (client_id, _normalized_claim), item in earliest.items():
        client = item["client"]
        if client_id not in client_cache:
            client_cache[client_id] = (_837_keys(client), _recon_keys(client))
        keys_837, keys_recon = client_cache[client_id]
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
        by_client[client_id].append({
            **item,
            "missing_in": missing_in,
            "missing_in_label": " and ".join(missing_in),
        })

    for items in by_client.values():
        items.sort(key=lambda item: (item["sent_at"], item["claim_number"]))
    return by_client


def _format_eastern(value) -> str:
    if not value:
        return "—"
    return value.astimezone(EASTERN).strftime("%m/%d/%Y, %I:%M:%S %p %Z")


def _email_content(client, items, now):
    rows = []
    for item in items:
        rows.append(
            "<tr>"
            f'<td style="padding:8px;border:1px solid #d7e0ea;font-weight:700;white-space:nowrap">{escape(item["claim_number"] or "—")}</td>'
            f'<td style="padding:8px;border:1px solid #d7e0ea">{escape(item["missing_in_label"])}</td>'
            f'<td style="padding:8px;border:1px solid #d7e0ea;white-space:nowrap">{escape(_format_eastern(item["sent_at"]))}</td>'
            f'<td style="padding:8px;border:1px solid #d7e0ea">{escape(item["mir_filename"] or "—")}</td>'
            "</tr>"
        )

    subject = f"OneSmarter: Daily Alert - {len(items)} Claim(s) Missing 837 / RECON After 7 Days"
    html = (
        f'<p>Dear {escape(client.name)} Team,</p>'
        '<p>The following successfully sent MIR claim(s) have reached the seven-day checkpoint and are still missing from the required 837 reference data, RECON data, or both.</p>'
        '<p>This is one consolidated daily alert. It will continue once per day after 5:30 PM Eastern until each claim is present in both 837 and RECON data.</p>'
        f'<p><strong>Alert generated:</strong> {escape(_format_eastern(now))}</p>'
        '<div style="overflow-x:auto"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:12px">'
        '<thead><tr><th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Claim</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Missing in</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">MIR sent</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">MIR file</th></tr></thead><tbody>'
        + "".join(rows)
        + '</tbody></table></div>'
        '<p style="margin-top:20px"><strong>Action required:</strong> Supply or ingest the missing 837/RECON claim data. The alert stops automatically when the claim is found in both sources.</p>'
    )
    body_text = "\n".join(
        f'{item["claim_number"]} | Missing in: {item["missing_in_label"]} | MIR sent: {_format_eastern(item["sent_at"])} | MIR: {item["mir_filename"]}'
        for item in items
    )
    return subject, html, body_text


def send_missing_reference_alerts(now=None) -> dict:
    """At/after 5:30 PM Eastern, send one client email daily until all missing data resolves."""
    now = now or timezone.now()
    local_now = now.astimezone(EASTERN)
    if local_now.time().replace(tzinfo=None) < SEND_AT:
        return {"emailed_claims": 0, "emails_sent": 0, "email_failures": 0, "before_send_time": True}

    grouped = _collect_due_missing(now)
    emailed_claims = 0
    emails_sent = 0
    email_failures = 0

    for items in grouped.values():
        if not items:
            continue
        client = items[0]["client"]
        recipients = alert_recipients(client)
        subject, html, body_text = _email_content(client, items, now)
        claim_payload = [
            {
                "claim_number": item["claim_number"],
                "claim_control_number": str(item["claim_control_number"] or "").strip(),
                "missing_in": list(item["missing_in"]),
                "missing_in_label": item["missing_in_label"],
                "sent_at": item["sent_at"].isoformat(),
                "eligible_at": item["eligible_at"].isoformat(),
                "mir_filename": item["mir_filename"],
            }
            for item in items
        ]
        audit = reserve_daily_alert(
            client=client,
            category=CATEGORY,
            alert_date=local_now.date(),
            subject=subject,
            recipients=recipients,
            claims=claim_payload,
            body_text=body_text,
        )
        if audit is None:
            continue

        try:
            sent = send_client_email(client, subject, html, to_emails=recipients)
        except Exception as exc:
            mark_alert_failed(audit, exc)
            email_failures += 1
            continue

        if sent:
            mark_alert_sent(audit, sent_at=now)
            emailed_claims += len(items)
            emails_sent += 1
        else:
            mark_alert_failed(audit, "Configured SMTP delivery returned false.")
            email_failures += 1

    return {
        "emailed_claims": emailed_claims,
        "emails_sent": emails_sent,
        "email_failures": email_failures,
        "before_send_time": False,
    }
