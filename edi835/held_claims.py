"""Fourth-day duplicate-claim quarantine and automatic held-claim release."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models.functions import Left
from django.utils import timezone


# Business rule is inclusive by calendar-day count: a claim successfully sent
# on the 1st becomes eligible to send again on the 4th at 5:30 PM Eastern.
DUPLICATE_HOLD_WINDOW = timedelta(days=3)
DUPLICATE_HISTORY_LOOKBACK = timedelta(days=4)
EASTERN_TIME_ZONE = ZoneInfo("America/New_York")
ELIGIBLE_SEND_HOUR = 17
ELIGIBLE_SEND_MINUTE = 30
RETRY_DELAY = timedelta(minutes=15)
DUPLICATE_HOLD_CODES = {"DUPLICATE_RECENT_MIR", "DUPLICATE_CLAIM_NUMBER"}
BLOCKING_SEVERITIES = {"HOLD", "REFUSE"}


def mir_claim_number(value) -> str:
    """Return the CLP01/MIR100 portion from the stored 23-character MIR key."""
    return str(value or "")[:17].strip()


def duplicate_eligible_send_at(sent_at):
    """Return 5:30 PM Eastern on the fourth calendar day of the hold."""
    if sent_at is None:
        return None
    if timezone.is_naive(sent_at):
        sent_at = timezone.make_aware(sent_at, dt_timezone.utc)
    sent_eastern = sent_at.astimezone(EASTERN_TIME_ZONE)
    eligible_date = sent_eastern.date() + DUPLICATE_HOLD_WINDOW
    eligible_eastern = datetime(
        eligible_date.year,
        eligible_date.month,
        eligible_date.day,
        ELIGIBLE_SEND_HOUR,
        ELIGIBLE_SEND_MINUTE,
        tzinfo=EASTERN_TIME_ZONE,
    )
    return eligible_eastern.astimezone(dt_timezone.utc)


def held_release_mir_filename(now=None) -> str:
    """Return the requested YYYYMMDDhhss.MIR release name in Eastern time."""
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, dt_timezone.utc)
    return now.astimezone(EASTERN_TIME_ZONE).strftime("%Y%m%d%H%S.MIR")


def recent_sent_claim_history(client, claim_numbers, now=None) -> dict[str, dict]:
    """Return recent sent matches using one DB-filtered lookup for incoming claims."""
    if client is None:
        return {}
    wanted = {str(value or "").strip() for value in claim_numbers if str(value or "").strip()}
    if not wanted:
        return {}

    from .models import MIRClaim

    now = now or timezone.now()
    cutoff = now - DUPLICATE_HISTORY_LOOKBACK
    # MIRClaim stores claim_control_number as CLP01+CLP07. Filter the first 17
    # characters in PostgreSQL instead of streaming every recently pushed claim
    # through Python. This keeps 1,000+ claim conversions fast as history grows.
    rows = (
        MIRClaim.objects.select_related("mir_file")
        .annotate(claim_number_key=Left("claim_control_number", 17))
        .filter(
            mir_file__client=client,
            mir_file__status="PUSHED",
            mir_file__updated_at__gt=cutoff,
            claim_number_key__in=wanted,
        )
        .order_by("-mir_file__updated_at")
    )

    result: dict[str, dict] = {}
    for row in rows.iterator(chunk_size=1000):
        claim_number = mir_claim_number(row.claim_control_number)
        if claim_number in result:
            continue
        sent_at = row.mir_file.updated_at
        eligible_send_at = duplicate_eligible_send_at(sent_at)
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


def _reschedule_pending_duplicates_bulk(client, claim_numbers, sent_at, mir_filename, exclude=None):
    """Reschedule many sent claim numbers with one held-file scan."""
    wanted = {str(value or "").strip() for value in claim_numbers if str(value or "").strip()}
    if client is None or not wanted:
        return
    from .models import EDI835File

    eligible = duplicate_eligible_send_at(sent_at)
    for source in EDI835File.objects.filter(client=client, held_claims_count__gt=0).iterator(chunk_size=100):
        findings = list(source.conversion_findings or [])
        changed = False
        for finding in findings:
            if str(finding.get("rule_code") or "") not in DUPLICATE_HOLD_CODES:
                continue
            if str(finding.get("claim_number") or "").strip() not in wanted:
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


def _reschedule_pending_duplicates(client, claim_number, sent_at, mir_filename, exclude=None):
    _reschedule_pending_duplicates_bulk(
        client,
        {claim_number},
        sent_at,
        mir_filename,
        exclude=exclude,
    )


def note_mir_sent(mir_file) -> None:
    """Record successful send time and activate/reschedule duplicate holds in bulk."""
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
        finding["eligible_send_at"] = duplicate_eligible_send_at(sent_at).isoformat()
        finding["previous_mir_filename"] = mir_file.mir_filename
        changed = True
    if changed:
        source.conversion_findings = findings
        source.held_claims_count = _recompute_held_count(findings)
        source.save(update_fields=["conversion_findings", "held_claims_count"])

    _reschedule_pending_duplicates_bulk(
        mir_file.client,
        sent_claim_numbers,
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


def _candidate_rows(now, limit=None):
    """Return every currently eligible claim unless an explicit test/admin limit is supplied."""
    from .models import EDI835File

    candidates = []
    scan_target = max(limit * 4, limit) if limit else None
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
            previous_sent = _parse_iso(finding.get("previous_sent_at"))
            eligible = (
                duplicate_eligible_send_at(previous_sent)
                if previous_sent
                else _parse_iso(finding.get("eligible_send_at"))
            )
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
            if scan_target and len(candidates) >= scan_target:
                break
        if scan_target and len(candidates) >= scan_target:
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
        if limit and len(selected) >= limit:
            break
    return selected


def _available_release_filename(now, MIRFile) -> str:
    """Keep the exact timestamp-shaped contract while preventing an SFTP overwrite."""
    candidate_time = now
    for _ in range(24 * 60 * 60):
        candidate = held_release_mir_filename(candidate_time)
        if not MIRFile.objects.filter(mir_filename=candidate).exists():
            return candidate
        candidate_time += timedelta(seconds=1)
    raise RuntimeError("Could not allocate a unique YYYYMMDDhhss MIR release filename.")


def release_due_held_claims(now=None, limit=None) -> dict:
    """Batch all eligible held claims per client and push each client batch to MIR outbound."""
    now = now or timezone.now()
    released = 0
    failed = 0
    files_sent = 0

    from admin_panel.mir_mapper_logic.edi835_parser import parse_835
    from admin_panel.mir_mapper_logic.mir_generator import generate_mir_text
    from .models import EDI835File, MIRFile
    from .mir_persistence import set_mir_push_status, store_mir_file
    from .services import upload_mir_to_sftp
    from .storage import relative_media_path, remove_delivered_outbound, write_mir_copies

    prepared_by_client = {}
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

            # Validate the claim by itself before allowing it into the daily batch.
            probe_text, probe_summary = generate_mir_text(
                [claim],
                client=source.client,
                process_date=now.astimezone(EASTERN_TIME_ZONE).date(),
            )
            if not probe_text or int(probe_summary.get("delivered_claims") or 0) != 1:
                reasons = [
                    str(item.get("reason") or item.get("rule_code") or "")
                    for item in (probe_summary.get("findings") or [])
                    if _blocking(item)
                ]
                raise ValueError("Held claim is still blocked: " + ("; ".join(reasons) or "unknown conversion hold"))

            group = prepared_by_client.setdefault(
                str(source.client_id),
                {"client": source.client, "items": []},
            )
            group["items"].append({"candidate": candidate, "source": source, "claim": claim})
        except Exception as exc:
            _mark_release_attempt(
                candidate["source_id"],
                candidate["claim_index"],
                status="RETRY",
                now=timezone.now(),
                error=str(exc),
            )
            failed += 1

    for group in prepared_by_client.values():
        client = group["client"]
        items = group["items"]
        claims = [item["claim"] for item in items]
        release_record = None
        mir_file = None
        out_path = None
        try:
            mir_text, summary = generate_mir_text(
                claims,
                client=client,
                process_date=now.astimezone(EASTERN_TIME_ZONE).date(),
            )
            delivered_claims = int(summary.get("delivered_claims") or 0)
            if not mir_text or delivered_claims != len(claims):
                reasons = [
                    str(item.get("reason") or item.get("rule_code") or "")
                    for item in (summary.get("findings") or [])
                    if _blocking(item)
                ]
                raise ValueError(
                    "Daily held-claim MIR batch did not contain every eligible claim: "
                    + ("; ".join(reasons) or f"expected {len(claims)}, delivered {delivered_claims}")
                )

            mir_filename = _available_release_filename(now, MIRFile)
            synthetic_835_name = f"held_release_{mir_filename[:-4]}.835"
            services_count = int(summary.get("delivered_services") or 0)
            if not services_count:
                services_count = sum(len(getattr(claim, "services", None) or []) for claim in claims)

            release_record = EDI835File.objects.create(
                client=client,
                original_filename=synthetic_835_name,
                stored_filename=synthetic_835_name,
                input_file_content="",
                status="PROCESSING",
                claims_count=len(claims),
                services_count=services_count,
                records_count=int(summary.get("mir_records") or len(claims)),
                delivered_claims_count=len(claims),
                held_claims_count=0,
                conversion_findings=[],
                processing_started_at=now,
                ingestion_source="HELD_RELEASE",
            )

            archive_path, out_path = write_mir_copies(client, mir_filename, mir_text)
            release_record.output_path = relative_media_path(archive_path)
            release_record.save(update_fields=["output_path"])
            mir_file = store_mir_file(
                source_835=release_record,
                mir_filename=mir_filename,
                mir_text=mir_text,
            )

            pushed = upload_mir_to_sftp(out_path, mir_filename, client=client)
            if not pushed:
                set_mir_push_status(mir_file, False)
                release_record.status = "ERROR"
                release_record.processing_completed_at = timezone.now()
                release_record.error_message = "Automatic daily held-claim MIR batch could not be pushed to outbound SFTP."
                release_record.save(update_fields=["status", "processing_completed_at", "error_message"])
                raise RuntimeError(release_record.error_message)

            sent_at = timezone.now()
            # Mark every source finding SENT before the PUSHED hook runs. The hook
            # can then reschedule later duplicates without re-holding this batch.
            for item in items:
                _mark_release_attempt(
                    item["source"].id,
                    item["candidate"]["claim_index"],
                    status="SENT",
                    now=sent_at,
                    mir_filename=mir_filename,
                )

            set_mir_push_status(mir_file, True)
            remove_delivered_outbound(client, "mir", out_path)
            release_record.status = "ARCHIVED"
            release_record.present_in_sftp = True
            release_record.processing_completed_at = sent_at
            release_record.save(update_fields=["status", "present_in_sftp", "processing_completed_at"])

            released += len(items)
            files_sent += 1
        except Exception as exc:
            if mir_file is not None and mir_file.status != "PUSH_FAILED":
                set_mir_push_status(mir_file, False)
            if release_record is not None and release_record.status != "ERROR":
                release_record.status = "ERROR"
                release_record.processing_completed_at = timezone.now()
                release_record.error_message = str(exc)[:2000]
                release_record.save(update_fields=["status", "processing_completed_at", "error_message"])
            for item in items:
                _mark_release_attempt(
                    item["candidate"]["source_id"],
                    item["candidate"]["claim_index"],
                    status="RETRY",
                    now=timezone.now(),
                    error=str(exc),
                )
            failed += len(items)

    return {"released": released, "failed": failed, "files_sent": files_sent}
