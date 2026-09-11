"""Four-day duplicate-claim quarantine and automatic held-claim release."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db import transaction
from django.utils import timezone


DUPLICATE_HOLD_WINDOW = timedelta(days=4)
RETRY_DELAY = timedelta(minutes=15)
DUPLICATE_HOLD_CODES = {"DUPLICATE_RECENT_MIR", "DUPLICATE_CLAIM_NUMBER"}
BLOCKING_SEVERITIES = {"HOLD", "REFUSE"}


def mir_claim_number(value) -> str:
    """Return the CLP01/MIR100 portion from the stored 23-character MIR key."""
    return str(value or "")[:17].strip()


def recent_sent_claim_history(client, claim_numbers, now=None) -> dict[str, dict]:
    """Return the most recent successfully sent MIR claim inside the four-day window."""
    if client is None:
        return {}
    wanted = {str(value or "").strip() for value in claim_numbers if str(value or "").strip()}
    if not wanted:
        return {}

    from .models import MIRClaim

    now = now or timezone.now()
    cutoff = now - DUPLICATE_HOLD_WINDOW
    rows = (
        MIRClaim.objects.select_related("mir_file")
        .filter(
            mir_file__client=client,
            mir_file__status="PUSHED",
            mir_file__updated_at__gt=cutoff,
        )
        .order_by("-mir_file__updated_at")
    )

    result: dict[str, dict] = {}
    for row in rows.iterator(chunk_size=1000):
        claim_number = mir_claim_number(row.claim_control_number)
        if claim_number not in wanted or claim_number in result:
            continue
        sent_at = row.mir_file.updated_at
        eligible_send_at = sent_at + DUPLICATE_HOLD_WINDOW
        if eligible_send_at <= now:
            continue
        result[claim_number] = {
            "claim_number": claim_number,
            "previous_sent_at": sent_at,
            "eligible_send_at": eligible_send_at,
            "previous_mir_filename": row.mir_file.mir_filename,
            "previous_mir_id": str(row.mir_file_id),
        }
        if len(result) == len(wanted):
            break
    return result


def _blocking(finding) -> bool:
    return str((finding or {}).get("severity") or "").upper() in BLOCKING_SEVERITIES


def _claim_key(finding):
    index = str((finding or {}).get("claim_index") or "").strip()
    if index:
        return ("index", index)
    return ("claim", str((finding or {}).get("claim_number") or "").strip())


def _recompute_held_count(findings) -> int:
    held = set()
    for finding in findings or []:
        if not _blocking(finding):
            continue
        key = _claim_key(finding)
        if key[1]:
            held.add(key)
    return len(held)


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, dt_timezone.utc)
    return parsed


def _same_occurrence(left, right) -> bool:
    left_index = str((left or {}).get("claim_index") or "").strip()
    right_index = str((right or {}).get("claim_index") or "").strip()
    if left_index and right_index:
        return left_index == right_index
    return (
        str((left or {}).get("claim_number") or "").strip()
        == str((right or {}).get("claim_number") or "").strip()
    )


def _has_other_blocking_finding(findings, candidate) -> bool:
    for finding in findings or []:
        if finding is candidate or not _blocking(finding):
            continue
        if not _same_occurrence(finding, candidate):
            continue
        if str(finding.get("rule_code") or "") not in DUPLICATE_HOLD_CODES:
            return True
    return False


def _reschedule_pending_duplicates(client, claim_number, sent_at, mir_filename, exclude=None):
    """Move every other pending copy of this claim to four days after the newest send."""
    if client is None or not claim_number:
        return
    from .models import EDI835File

    eligible = sent_at + DUPLICATE_HOLD_WINDOW
    for source in EDI835File.objects.filter(client=client, held_claims_count__gt=0).iterator(chunk_size=100):
        findings = list(source.conversion_findings or [])
        changed = False
        for finding in findings:
            if str(finding.get("rule_code") or "") not in DUPLICATE_HOLD_CODES:
                continue
            if str(finding.get("claim_number") or "").strip() != claim_number:
                continue
            if str(finding.get("release_status") or "").upper() == "SENT":
                continue
            marker = (str(source.id), str(finding.get("claim_index") or ""))
            if exclude and marker == exclude:
                continue
            finding["severity"] = "HOLD"
            finding["release_status"] = "HELD"
            finding["previous_sent_at"] = sent_at.isoformat()
            finding["eligible_send_at"] = eligible.isoformat()
            finding["previous_mir_filename"] = mir_filename
            finding.pop("last_release_error", None)
            changed = True
        if changed:
            source.conversion_findings = findings
            source.held_claims_count = _recompute_held_count(findings)
            source.save(update_fields=["conversion_findings", "held_claims_count"])


def note_mir_sent(mir_file) -> None:
    """Record the exact successful send time and activate/reschedule duplicate holds."""
    sent_at = mir_file.updated_at or timezone.now()
    source = mir_file.source_835
    sent_claim_numbers = {
        mir_claim_number(value)
        for value in mir_file.claims.values_list("claim_control_number", flat=True)
        if mir_claim_number(value)
    }

    findings = list(source.conversion_findings or [])
    changed = False
    for finding in findings:
        if str(finding.get("rule_code") or "") != "DUPLICATE_CLAIM_NUMBER":
            continue
        if not _blocking(finding):
            continue
        claim_number = str(finding.get("claim_number") or "").strip()
        if claim_number not in sent_claim_numbers:
            continue
        finding["release_status"] = "HELD"
        finding["previous_sent_at"] = sent_at.isoformat()
        finding["eligible_send_at"] = (sent_at + DUPLICATE_HOLD_WINDOW).isoformat()
        finding["previous_mir_filename"] = mir_file.mir_filename
        changed = True
    if changed:
        source.conversion_findings = findings
        source.held_claims_count = _recompute_held_count(findings)
        source.save(update_fields=["conversion_findings", "held_claims_count"])

    for claim_number in sent_claim_numbers:
        _reschedule_pending_duplicates(
            mir_file.client,
            claim_number,
            sent_at,
            mir_file.mir_filename,
        )


def _mark_release_attempt(source_id, claim_index, *, status, now, error="", mir_filename=""):
    from .models import EDI835File

    with transaction.atomic():
        source = EDI835File.objects.select_for_update().get(id=source_id)
        findings = list(source.conversion_findings or [])
        targets = [
            finding for finding in findings
            if str(finding.get("rule_code") or "") in DUPLICATE_HOLD_CODES
            and str(finding.get("claim_index") or "") == str(claim_index or "")
        ]
        if not targets:
            return None

        for target in targets:
            target["last_release_attempt_at"] = now.isoformat()
            if status == "SENT":
                target["severity"] = "INFO"
                target["release_status"] = "SENT"
                target["released_at"] = now.isoformat()
                target["release_mir_filename"] = mir_filename
                target.pop("last_release_error", None)
            else:
                target["release_status"] = "RETRY"
                target["last_release_error"] = str(error or "Held-claim release failed.")[:1000]

        source.conversion_findings = findings
        source.held_claims_count = _recompute_held_count(findings)
        source.save(update_fields=["conversion_findings", "held_claims_count"])
        return source.client_id, str(targets[0].get("claim_number") or "").strip()


def _candidate_rows(now, limit):
    from .models import EDI835File

    candidates = []
    sources = (
        EDI835File.objects.select_related("client")
        .filter(held_claims_count__gt=0)
        .exclude(conversion_findings=[])
        .order_by("uploaded_at")
    )
    for source in sources.iterator(chunk_size=100):
        findings = list(source.conversion_findings or [])
        for finding in findings:
            if str(finding.get("rule_code") or "") not in DUPLICATE_HOLD_CODES:
                continue
            if not _blocking(finding):
                continue
            if str(finding.get("release_status") or "").upper() not in {"HELD", "RETRY"}:
                continue
            eligible = _parse_iso(finding.get("eligible_send_at"))
            if eligible is None or eligible > now:
                continue
            last_attempt = _parse_iso(finding.get("last_release_attempt_at"))
            if str(finding.get("release_status") or "").upper() == "RETRY" and last_attempt and last_attempt + RETRY_DELAY > now:
                continue
            if _has_other_blocking_finding(findings, finding):
                continue
            claim_index = str(finding.get("claim_index") or "").strip()
            claim_number = str(finding.get("claim_number") or "").strip()
            if not claim_index or not claim_number or not source.input_file_content:
                continue
            candidates.append({
                "source_id": str(source.id),
                "client_id": str(source.client_id or ""),
                "claim_index": claim_index,
                "claim_number": claim_number,
                "eligible_send_at": eligible,
            })
            if len(candidates) >= max(limit * 4, limit):
                break
        if len(candidates) >= max(limit * 4, limit):
            break

    candidates.sort(key=lambda item: item["eligible_send_at"])
    selected = []
    seen = set()
    for item in candidates:
        key = (item["client_id"], item["claim_number"])
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def release_due_held_claims(now=None, limit=25) -> dict:
    """Release due duplicate claims into heldclaims_*.MIR files and send them outbound."""
    now = now or timezone.now()
    released = 0
    failed = 0

    from admin_panel.mir_mapper_logic.edi835_parser import parse_835
    from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
    from .models import EDI835File
    from .mir_persistence import set_mir_push_status, store_mir_file
    from .services import upload_mir_to_sftp
    from .storage import relative_media_path, remove_delivered_outbound, write_mir_copies

    for candidate in _candidate_rows(now, limit):
        source = EDI835File.objects.select_related("client").filter(id=candidate["source_id"]).first()
        if source is None or source.client is None:
            continue
        try:
            claims = parse_835(source.input_file_content)
            index = int(candidate["claim_index"]) - 1
            if index < 0 or index >= len(claims):
                raise ValueError("Held claim can no longer be located in the original 835 source.")
            claim = claims[index]
            if str(claim.claim_number or "").strip() != candidate["claim_number"]:
                raise ValueError("Held claim index no longer matches the recorded claim number.")

            mir_text, summary = generate_mir_text([claim], client=source.client, process_date=timezone.localdate())
            if not mir_text or int(summary.get("delivered_claims") or 0) != 1:
                reasons = [
                    str(item.get("reason") or item.get("rule_code") or "")
                    for item in (summary.get("findings") or [])
                    if _blocking(item)
                ]
                raise ValueError("Held claim is still blocked: " + ("; ".join(reasons) or "unknown conversion hold"))

            stamp = timezone.localtime(now).strftime("%Y%m%d_%H%M%S")
            token = uuid.uuid4().hex[:8]
            stem = f"heldclaims_{stamp}_{token}"
            mir_filename = f"{stem}.MIR"
            synthetic_835_name = f"{stem}.835"

            release_record = EDI835File.objects.create(
                client=source.client,
                original_filename=synthetic_835_name,
                stored_filename=synthetic_835_name,
                input_file_content="",
                status="PROCESSING",
                claims_count=1,
                services_count=int(summary.get("delivered_services") or len(claim.services or [])),
                records_count=int(summary.get("mir_records") or 1),
                delivered_claims_count=1,
                held_claims_count=0,
                conversion_findings=[],
                processing_started_at=now,
                ingestion_source="HELD_RELEASE",
            )

            archive_path, out_path = write_mir_copies(source.client, mir_filename, mir_text)
            release_record.output_path = relative_media_path(archive_path)
            release_record.save(update_fields=["output_path"])
            mir_file = store_mir_file(
                source_835=release_record,
                mir_filename=mir_filename,
                mir_text=mir_text,
            )

            pushed = upload_mir_to_sftp(out_path, mir_filename, client=source.client)
            if not pushed:
                set_mir_push_status(mir_file, False)
                release_record.status = "ERROR"
                release_record.processing_completed_at = timezone.now()
                release_record.error_message = "Automatic held-claim MIR release could not be pushed to outbound SFTP."
                release_record.save(update_fields=["status", "processing_completed_at", "error_message"])
                raise RuntimeError(release_record.error_message)

            sent_at = timezone.now()
            # The SFTP upload has completed at this point, so mark this source
            # occurrence sent before the global PUSHED hook reschedules other
            # pending copies of the same claim number.
            _mark_release_attempt(
                source.id,
                candidate["claim_index"],
                status="SENT",
                now=sent_at,
                mir_filename=mir_filename,
            )
            set_mir_push_status(mir_file, True)
            remove_delivered_outbound(source.client, "mir", out_path)
            release_record.status = "ARCHIVED"
            release_record.present_in_sftp = True
            release_record.processing_completed_at = sent_at
            release_record.save(update_fields=["status", "present_in_sftp", "processing_completed_at"])

            _reschedule_pending_duplicates(
                source.client,
                candidate["claim_number"],
                sent_at,
                mir_filename,
                exclude=(str(source.id), str(candidate["claim_index"])),
            )
            released += 1
        except Exception as exc:
            _mark_release_attempt(
                candidate["source_id"],
                candidate["claim_index"],
                status="RETRY",
                now=timezone.now(),
                error=str(exc),
            )
            failed += 1

    return {"released": released, "failed": failed}
