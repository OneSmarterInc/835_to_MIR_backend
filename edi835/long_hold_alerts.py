"""Escalation emails for non-duplicate claims that remain held more than seven days."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from admin_panel.email_service import get_client_users, send_client_email
from .held_claims import mir_claim_number
from .models import EDI835File


EASTERN = ZoneInfo("America/New_York")
LONG_HOLD_AGE = timedelta(days=7)
MAX_ALERT_DAYS = 7
BLOCKING_SEVERITIES = {"HOLD", "REFUSE"}

# Keep the original field for backward compatibility with alerts already sent
# before the daily escalation policy was introduced.
ALERT_FIELD = "seven_day_hold_alert_sent_at"
ALERT_COUNT_FIELD = "seven_day_hold_alert_count"
ALERT_LAST_FIELD = "seven_day_hold_last_alert_sent_at"
RESOLUTION_STATUS_FIELD = "hold_resolution_status"
RESOLVED_AT_FIELD = "hold_resolved_at"
RESOLVED_MIR_FIELD = "hold_resolved_mir_filename"
RESOLVED_SOURCE_FIELD = "hold_resolved_source_835_filename"


def _is_nonduplicate_blocking(finding) -> bool:
    severity = str((finding or {}).get("severity") or "").upper()
    code = str((finding or {}).get("rule_code") or (finding or {}).get("rule_name") or "").upper()
    return severity in BLOCKING_SEVERITIES and not code.startswith("DUPLICATE")


def _is_resolved(finding) -> bool:
    return str((finding or {}).get(RESOLUTION_STATUS_FIELD) or "").upper() == "RESOLVED"


def _claim_key(finding):
    claim_index = str((finding or {}).get("claim_index") or "").strip()
    if claim_index:
        return ("index", claim_index)
    claim_number = _finding_claim_number(finding)
    return ("claim", claim_number)


def _finding_claim_number(finding) -> str:
    value = str(
        (finding or {}).get("claim_number")
        or (finding or {}).get("claim_control_number")
        or ""
    ).strip()
    return mir_claim_number(value) if value else ""


def _same_claim(finding, key) -> bool:
    return _claim_key(finding) == key


def _parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _format_eastern(value) -> str:
    value = _parse_datetime(value)
    if not value:
        return "—"
    try:
        return value.astimezone(EASTERN).strftime("%m/%d/%Y %I:%M:%S %p %Z")
    except Exception:
        return str(value)


def _current_held_count(findings) -> int:
    """Count claim occurrences that still have an unresolved blocking finding."""
    held = set()
    for finding in findings or []:
        severity = str((finding or {}).get("severity") or "").upper()
        if severity not in BLOCKING_SEVERITIES:
            continue
        if _is_nonduplicate_blocking(finding) and _is_resolved(finding):
            continue
        key = _claim_key(finding)
        if key[1]:
            held.add(key)
    return len(held)


def _claim_alert_state(findings) -> tuple[int, datetime | None]:
    count = 0
    last = None
    for finding in findings:
        try:
            count = max(count, int(finding.get(ALERT_COUNT_FIELD) or 0))
        except (TypeError, ValueError):
            pass
        candidate = _parse_datetime(finding.get(ALERT_LAST_FIELD) or finding.get(ALERT_FIELD))
        if candidate and (last is None or candidate > last):
            last = candidate

    # Existing one-time alerts predate ALERT_COUNT_FIELD. Treat them as day 1.
    if count == 0 and last is not None:
        count = 1
    return count, last


def _mark_source_claims_resolved(source, claim_numbers: set[str], *, resolved_at, mir_file) -> int:
    """Resolve matching non-duplicate holds on one historical 835 source."""
    findings = list(source.conversion_findings or [])
    changed = False
    resolved_claim_keys = set()
    sent_source_name = getattr(getattr(mir_file, "source_835", None), "original_filename", "") or ""

    for finding in findings:
        if not _is_nonduplicate_blocking(finding) or _is_resolved(finding):
            continue
        claim_number = _finding_claim_number(finding)
        if not claim_number or claim_number not in claim_numbers:
            continue
        finding[RESOLUTION_STATUS_FIELD] = "RESOLVED"
        finding[RESOLVED_AT_FIELD] = resolved_at.isoformat()
        finding[RESOLVED_MIR_FIELD] = mir_file.mir_filename
        finding[RESOLVED_SOURCE_FIELD] = sent_source_name
        changed = True
        resolved_claim_keys.add(_claim_key(finding))

    if changed:
        source.conversion_findings = findings
        source.held_claims_count = _current_held_count(findings)
        source.save(update_fields=["conversion_findings", "held_claims_count"])
    return len(resolved_claim_keys)


def mark_nonduplicate_holds_resolved_by_push(mir_file) -> int:
    """Stop escalation when the same claim is later included in another PUSHED MIR."""
    client = getattr(mir_file, "client", None)
    source_835 = getattr(mir_file, "source_835", None)
    if client is None:
        return 0

    claim_numbers = {
        mir_claim_number(value)
        for value in mir_file.claims.values_list("claim_control_number", flat=True)
        if mir_claim_number(value)
    }
    if not claim_numbers:
        return 0

    resolved_at = getattr(mir_file, "updated_at", None) or timezone.now()
    resolved = 0
    sources = (
        EDI835File.objects.select_related("client")
        .filter(client=client)
        .exclude(conversion_findings=[])
        .order_by("uploaded_at")
    )
    if source_835 is not None:
        sources = sources.exclude(id=source_835.id)

    for source in sources.iterator(chunk_size=100):
        held_since = source.processing_completed_at or source.uploaded_at
        if held_since and resolved_at <= held_since:
            continue
        with transaction.atomic():
            locked = EDI835File.objects.select_for_update().get(id=source.id)
            resolved += _mark_source_claims_resolved(
                locked,
                claim_numbers,
                resolved_at=resolved_at,
                mir_file=mir_file,
            )
    return resolved


def _collect_overdue(now):
    """Return unresolved non-duplicate holds due for today's escalation email."""
    grouped = defaultdict(list)
    oldest_allowed = now - LONG_HOLD_AGE
    today_eastern = now.astimezone(EASTERN).date()

    sources = (
        EDI835File.objects.select_related("client")
        .filter(uploaded_at__lt=oldest_allowed)
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
            if _is_nonduplicate_blocking(finding) and not _is_resolved(finding):
                key = _claim_key(finding)
                if key[1]:
                    by_claim[key].append(finding)

        for key, claim_findings in by_claim.items():
            alert_count, last_alert = _claim_alert_state(claim_findings)
            if alert_count >= MAX_ALERT_DAYS:
                continue
            if last_alert and last_alert.astimezone(EASTERN).date() >= today_eastern:
                continue

            first = claim_findings[0]
            claim_number = _finding_claim_number(first) or key[1]
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
                "alert_number": alert_count + 1,
                "alert_limit": MAX_ALERT_DAYS,
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
            f'<td style="padding:8px;border:1px solid #d7e0ea;text-align:center">{item["alert_number"]}/{item["alert_limit"]}</td>'
            f'<td style="padding:8px;border:1px solid #d7e0ea">{reasons_html}</td>'
            "</tr>"
        )

    subject = f"OneSmarter: Daily Alert - {len(items)} Unresolved Claim(s) Held More Than 7 Days"
    html = (
        f'<p>Dear {escape(client.name)} Team,</p>'
        '<p>The following claim(s) remain on a non-duplicate conversion hold for more than seven days and require review.</p>'
        '<p>This alert is sent once per day for up to seven alert days. Alerts stop immediately if the same claim is later included in another MIR that is successfully pushed.</p>'
        f'<p><strong>Alert generated:</strong> {escape(_format_eastern(now))}</p>'
        '<div style="overflow-x:auto"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:12px">'
        '<thead><tr>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Claim</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Held from 835</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Held since</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:right;background:#eef3f8">Days held</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:center;background:#eef3f8">Alert day</th>'
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
            for key in keys:
                matching = [
                    finding for finding in findings
                    if _is_nonduplicate_blocking(finding)
                    and not _is_resolved(finding)
                    and _same_claim(finding, key)
                ]
                if not matching:
                    continue
                count, _last = _claim_alert_state(matching)
                new_count = min(MAX_ALERT_DAYS, count + 1)
                for finding in matching:
                    if not finding.get(ALERT_FIELD):
                        finding[ALERT_FIELD] = sent_at.isoformat()
                    finding[ALERT_COUNT_FIELD] = new_count
                    finding[ALERT_LAST_FIELD] = sent_at.isoformat()
                    changed = True
            if changed:
                source.conversion_findings = findings
                source.save(update_fields=["conversion_findings"])


def send_overdue_nonduplicate_hold_alerts(now=None) -> dict:
    """Send each unresolved claim at most once per Eastern day for seven alert days."""
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
