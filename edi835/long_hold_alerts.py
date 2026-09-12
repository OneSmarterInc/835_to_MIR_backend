"""Email alerts for non-duplicate claims that remain held more than seven days."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from html import escape
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from admin_panel.email_service import get_client_users, send_client_email
from .models import EDI835File


EASTERN = ZoneInfo("America/New_York")
LONG_HOLD_AGE = timedelta(days=7)
ALERT_FIELD = "seven_day_hold_alert_sent_at"
BLOCKING_SEVERITIES = {"HOLD", "REFUSE"}


def _is_nonduplicate_blocking(finding) -> bool:
    severity = str((finding or {}).get("severity") or "").upper()
    code = str((finding or {}).get("rule_code") or (finding or {}).get("rule_name") or "").upper()
    return severity in BLOCKING_SEVERITIES and not code.startswith("DUPLICATE")


def _claim_key(finding):
    claim_index = str((finding or {}).get("claim_index") or "").strip()
    if claim_index:
        return ("index", claim_index)
    claim_number = str(
        (finding or {}).get("claim_number")
        or (finding or {}).get("claim_control_number")
        or ""
    ).strip()
    return ("claim", claim_number)


def _same_claim(finding, key) -> bool:
    return _claim_key(finding) == key


def _format_eastern(value) -> str:
    if not value:
        return "—"
    try:
        if timezone.is_naive(value):
            value = timezone.make_aware(value)
        return value.astimezone(EASTERN).strftime("%m/%d/%Y %I:%M:%S %p %Z")
    except Exception:
        return str(value)


def _collect_overdue(now):
    """Return newly overdue non-duplicate held claims grouped by client."""
    grouped = defaultdict(list)
    oldest_allowed = now - LONG_HOLD_AGE

    sources = (
        EDI835File.objects.select_related("client")
        .filter(held_claims_count__gt=0, uploaded_at__lt=oldest_allowed)
        .exclude(conversion_findings=[])
        .order_by("uploaded_at")
    )

    for source in sources.iterator(chunk_size=100):
        if source.client is None:
            continue
        held_since = source.processing_completed_at or source.uploaded_at
        if not held_since or held_since + LONG_HOLD_AGE >= now:
            continue

        findings = list(source.conversion_findings or [])
        by_claim = defaultdict(list)
        for finding in findings:
            if _is_nonduplicate_blocking(finding):
                key = _claim_key(finding)
                if key[1]:
                    by_claim[key].append(finding)

        for key, claim_findings in by_claim.items():
            # One email per claim after it first crosses seven days. The marker
            # lives in the existing findings JSON, so no schema migration is needed.
            if any(finding.get(ALERT_FIELD) for finding in claim_findings):
                continue

            first = claim_findings[0]
            claim_number = str(
                first.get("claim_number")
                or first.get("claim_control_number")
                or key[1]
            ).strip()
            reasons = []
            for finding in claim_findings:
                code = str(finding.get("rule_code") or finding.get("rule_name") or "HOLD").strip()
                reason = str(finding.get("reason") or finding.get("message") or "Claim remains held.").strip()
                text = f"{code}: {reason}" if code else reason
                if text not in reasons:
                    reasons.append(text)

            grouped[str(source.client_id)].append({
                "client": source.client,
                "source_id": str(source.id),
                "claim_key": key,
                "claim_number": claim_number,
                "source_835_filename": source.original_filename or source.stored_filename or "",
                "held_since": held_since,
                "days_held": max(7, int((now - held_since).total_seconds() // 86400)),
                "reasons": reasons,
            })

    return grouped


def _send_client_alert(client, items, now) -> bool:
    rows = []
    for item in items:
        reasons_html = "<br/>".join(escape(reason) for reason in item["reasons"])
        rows.append(
            "<tr>"
            f'<td style="padding:8px;border:1px solid #d7e0ea;font-weight:700;white-space:nowrap">{escape(item["claim_number"] or "—")}</td>'
            f'<td style="padding:8px;border:1px solid #d7e0ea">{escape(item["source_835_filename"] or "—")}</td>'
            f'<td style="padding:8px;border:1px solid #d7e0ea;white-space:nowrap">{escape(_format_eastern(item["held_since"]))}</td>'
            f'<td style="padding:8px;border:1px solid #d7e0ea;text-align:right">{item["days_held"]}</td>'
            f'<td style="padding:8px;border:1px solid #d7e0ea">{reasons_html}</td>'
            "</tr>"
        )

    subject = f"OneSmarter: {len(items)} Claim(s) Held More Than 7 Days"
    html = (
        f'<p>Dear {escape(client.name)} Team,</p>'
        '<p>The following claim(s) have remained on a non-duplicate conversion hold for more than seven days and require review.</p>'
        f'<p><strong>Alert generated:</strong> {escape(_format_eastern(now))}</p>'
        '<div style="overflow-x:auto"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:12px">'
        '<thead><tr>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Claim</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Source 835</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Held since</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:right;background:#eef3f8">Days held</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Hold reason</th>'
        '</tr></thead><tbody>'
        + "".join(rows)
        + '</tbody></table></div>'
        '<p style="margin-top:20px"><strong>Action recommended:</strong> Review and resolve the listed non-duplicate hold reasons.</p>'
    )

    recipients = set(get_client_users(client))
    if getattr(client, "email", ""):
        recipients.add(client.email)
    return send_client_email(client, subject, html, to_emails=sorted(recipients))


def _mark_alerted(items, sent_at) -> None:
    by_source = defaultdict(list)
    for item in items:
        by_source[item["source_id"]].append(item["claim_key"])

    for source_id, keys in by_source.items():
        with transaction.atomic():
            source = EDI835File.objects.select_for_update().get(id=source_id)
            findings = list(source.conversion_findings or [])
            changed = False
            for finding in findings:
                if not _is_nonduplicate_blocking(finding):
                    continue
                if any(_same_claim(finding, key) for key in keys):
                    finding[ALERT_FIELD] = sent_at.isoformat()
                    changed = True
            if changed:
                source.conversion_findings = findings
                source.save(update_fields=["conversion_findings"])


def send_overdue_nonduplicate_hold_alerts(now=None) -> dict:
    """Email each client once for newly-overdue non-duplicate held claims."""
    now = now or timezone.now()
    grouped = _collect_overdue(now)
    emailed_claims = 0
    emails_sent = 0
    email_failures = 0

    for items in grouped.values():
        if not items:
            continue
        client = items[0]["client"]
        if _send_client_alert(client, items, now):
            _mark_alerted(items, now)
            emailed_claims += len(items)
            emails_sent += 1
        else:
            email_failures += 1

    return {
        "emailed_claims": emailed_claims,
        "emails_sent": emails_sent,
        "email_failures": email_failures,
    }
