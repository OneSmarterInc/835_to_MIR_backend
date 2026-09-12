"""Email notification for successful held-claim MIR releases."""

from __future__ import annotations

from html import escape
from zoneinfo import ZoneInfo

from django.utils import timezone

from admin_panel.email_service import get_client_users, send_client_email

from .held_claims import DUPLICATE_HOLD_CODES, mir_claim_number
from .models import EDI835File


EASTERN = ZoneInfo("America/New_York")


def _format_eastern(value) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            from datetime import datetime

            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return str(value)
    try:
        if timezone.is_naive(value):
            value = timezone.make_aware(value)
        return value.astimezone(EASTERN).strftime("%m/%d/%Y %I:%M:%S %p %Z")
    except Exception:
        return str(value)


def _release_provenance(client, mir_filename: str, claim_numbers: set[str]) -> dict[str, dict]:
    """Return exact source hold details for claims released in one MIR file."""
    if client is None or not mir_filename or not claim_numbers:
        return {}

    result: dict[str, dict] = {}
    rows = (
        EDI835File.objects.filter(client=client)
        .exclude(ingestion_source="HELD_RELEASE")
        .exclude(conversion_findings=[])
        .order_by("-uploaded_at")
        .values("original_filename", "conversion_findings")
    )

    for row in rows.iterator(chunk_size=100):
        for finding in row.get("conversion_findings") or []:
            if str(finding.get("rule_code") or "") not in DUPLICATE_HOLD_CODES:
                continue
            claim_number = str(finding.get("claim_number") or "").strip()
            if claim_number not in claim_numbers or claim_number in result:
                continue
            if str(finding.get("release_status") or "").upper() != "SENT":
                continue
            if str(finding.get("release_mir_filename") or "").strip() != mir_filename:
                continue
            result[claim_number] = {
                "source_835_filename": row.get("original_filename") or "",
                "held_from_mir": str(finding.get("previous_mir_filename") or ""),
                "previous_sent_at": finding.get("previous_sent_at"),
                "eligible_send_at": finding.get("eligible_send_at"),
                "released_at": finding.get("released_at"),
            }
        if len(result) == len(claim_numbers):
            break
    return result


def send_held_release_sftp_notice(mir_file) -> bool:
    """Notify the client after an eligible held-claim MIR is pushed to SFTP."""
    source = getattr(mir_file, "source_835", None)
    client = getattr(mir_file, "client", None) or getattr(source, "client", None)
    if source is None or str(getattr(source, "ingestion_source", "") or "").upper() != "HELD_RELEASE":
        return False
    if client is None:
        return False

    mir_claims = list(mir_file.claims.all().order_by("claim_sequence"))
    claim_numbers = {
        mir_claim_number(claim.claim_control_number)
        for claim in mir_claims
        if mir_claim_number(claim.claim_control_number)
    }
    provenance = _release_provenance(client, mir_file.mir_filename, claim_numbers)

    rows = []
    total_services = 0
    for claim in mir_claims:
        claim_number = mir_claim_number(claim.claim_control_number)
        details = provenance.get(claim_number, {})
        service_count = int(claim.service_count or 0)
        total_services += service_count
        rows.append(
            "<tr>"
            f'<td style="padding:8px;border:1px solid #d7e0ea;font-weight:700;white-space:nowrap">{escape(claim_number or claim.claim_control_number or "—")}</td>'
            f'<td style="padding:8px;border:1px solid #d7e0ea">{escape(details.get("source_835_filename") or "—")}</td>'
            f'<td style="padding:8px;border:1px solid #d7e0ea">{escape(details.get("held_from_mir") or "—")}</td>'
            f'<td style="padding:8px;border:1px solid #d7e0ea;white-space:nowrap">{escape(_format_eastern(details.get("previous_sent_at")))}</td>'
            f'<td style="padding:8px;border:1px solid #d7e0ea;white-space:nowrap">{escape(_format_eastern(details.get("eligible_send_at")))}</td>'
            f'<td style="padding:8px;border:1px solid #d7e0ea;text-align:right">{service_count}</td>'
            "</tr>"
        )

    released_at = getattr(source, "processing_completed_at", None) or getattr(mir_file, "updated_at", None) or timezone.now()
    subject = f"OneSmarter: Held Claims Released to SFTP - {mir_file.mir_filename}"
    html = (
        f'<p>Dear {escape(client.name)} Team,</p>'
        '<p>An eligible held-claim MIR batch was successfully pushed to the configured MIR outbound SFTP location.</p>'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin:18px 0">'
        f'<tr><td style="padding:10px 12px;border:1px solid #d7e0ea;background:#f6f9fc;font-weight:700;width:38%">Release MIR file</td><td style="padding:10px 12px;border:1px solid #d7e0ea">{escape(mir_file.mir_filename)}</td></tr>'
        f'<tr><td style="padding:10px 12px;border:1px solid #d7e0ea;background:#f6f9fc;font-weight:700">SFTP status</td><td style="padding:10px 12px;border:1px solid #d7e0ea">PUSHED</td></tr>'
        f'<tr><td style="padding:10px 12px;border:1px solid #d7e0ea;background:#f6f9fc;font-weight:700">Claims sent</td><td style="padding:10px 12px;border:1px solid #d7e0ea">{len(mir_claims)}</td></tr>'
        f'<tr><td style="padding:10px 12px;border:1px solid #d7e0ea;background:#f6f9fc;font-weight:700">Service lines sent</td><td style="padding:10px 12px;border:1px solid #d7e0ea">{total_services}</td></tr>'
        f'<tr><td style="padding:10px 12px;border:1px solid #d7e0ea;background:#f6f9fc;font-weight:700">Released at</td><td style="padding:10px 12px;border:1px solid #d7e0ea">{escape(_format_eastern(released_at))}</td></tr>'
        '</table>'
        '<h3 style="margin:24px 0 10px">Claims included in this release</h3>'
        '<div style="overflow-x:auto"><table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;font-size:12px">'
        '<thead><tr>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Claim</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Source 835</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Held back from MIR</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Previously sent</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:left;background:#eef3f8">Eligible</th>'
        '<th style="padding:8px;border:1px solid #d7e0ea;text-align:right;background:#eef3f8">Services</th>'
        '</tr></thead><tbody>'
        + "".join(rows)
        + '</tbody></table></div>'
        '<p style="margin-top:20px">No action is required. This message is the delivery record for the held-claim release.</p>'
    )

    recipients = set(get_client_users(client))
    if getattr(client, "email", ""):
        recipients.add(client.email)
    return send_client_email(
        client,
        subject,
        html,
        to_emails=sorted(recipients),
    )
