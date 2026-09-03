"""RECON upload parsing and normalized database persistence."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import uuid
from collections import OrderedDict
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from .models import (
    MIRClaim,
    RECONClaim,
    RECONFile,
    RECONProcessingError,
    RECONProcessingRun,
    RECONServiceLine,
)


ALIASES = {
    "claim_control_number": {"claimid", "claimnumber", "claimcontrolnumber", "claim", "icn", "clp01", "claimno", "claimidentifier"},
    "member_id": {"memberid", "subscriberid", "member", "patientid"},
    "patient_control_number": {"patientcontrolnumber", "patientaccountnumber", "pcn"},
    "record_type": {"recordtype", "type"},
    "claim_status": {"claimstatus", "status"},
    "service_line_number": {"servicelinenumber", "linenumber", "line", "serviceline"},
    "procedure_code": {"procedurecode", "procedure", "cpt", "hcpcs"},
    "revenue_code": {"revenuecode", "revenue"},
    "service_from_date": {"servicefromdate", "fromdate", "servicedate", "dosfrom"},
    "service_to_date": {"servicetodate", "todate", "dosto"},
    "units": {"units", "serviceunits"},
    "charge_amount": {"chargeamount", "chargedamount", "billedamount", "totalcharge"},
    "allowed_amount": {"allowedamount", "approvedamount"},
    "paid_amount": {"paidamount", "paymentamount", "totalpaid", "claimpaidamount", "netpaidamount", "checkamount", "amountinrecon", "reconamount"},
    "mir904_bluecard_fee": {"mir904", "bluecardaccessfee", "bluecardfee"},
    "mir905_aea": {"mir905", "administrativeexpenseallowance", "aea"},
    "mir907_amount": {"mir907"},
    "mir908_amount": {"mir908"},
    "mpl920_pca_fee": {"mpl920", "pcafee"},
    "patient_responsibility": {"patientresponsibility", "patientamount", "patientliability"},
    "adjustment_amount": {"adjustmentamount", "adjustedamount"},
    "reason_code": {"reasoncode", "adjustmentreason", "remarkcode"},
}


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _canonical_row(row: dict) -> dict:
    normalized = {_key(k): str(v or "").strip() for k, v in row.items()}
    result = {}
    for field, aliases in ALIASES.items():
        result[field] = next((normalized[a] for a in aliases if normalized.get(a)), "")
    return result


def _decimal(value: str) -> Decimal:
    cleaned = re.sub(r"[^0-9.()\-+]", "", str(value or "").strip())
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return Decimal(cleaned or "0")
    except InvalidOperation:
        return Decimal("0")


def _money_decimal(value: str) -> Decimal:
    """Parse either ordinary decimal money or MIR implied cents + trailing sign."""
    raw = str(value or "").strip()
    if re.fullmatch(r"\d+[+-]", raw):
        sign = Decimal("-1") if raw[-1] == "-" else Decimal("1")
        return sign * Decimal(raw[:-1] or "0").scaleb(-2)
    return _decimal(raw)


def _valid_money(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return True
    if re.fullmatch(r"\d+[+-]", raw):
        return True
    normalized = raw.replace("$", "").replace(",", "")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = "-" + normalized[1:-1]
    try:
        Decimal(normalized)
        return True
    except InvalidOperation:
        return False


def _detect_delimiter(text: str) -> str | None:
    sample = "\n".join(text.splitlines()[:10])
    for delimiter in (",", "\t", "|", ";"):
        if delimiter in sample:
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",\t|;")
                return dialect.delimiter
            except csv.Error:
                return delimiter
    return None


def _claim_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _fixed_width_data(raw: str, row_number: int, known_claim_ids=None) -> dict | None:
    # A reference MIR/RECON physical row uses the same 334-byte header and
    # 303-byte service blocks as MIR. Its complete reconciliation key occupies
    # positions 3-25 (MIR100 plus the cross-reference).
    if len(raw) >= 334 and (len(raw) - 334) % 303 == 0:
        service_count = _integer(raw[332:334])
        actual_count = (len(raw) - 334) // 303
        if service_count == actual_count:
            return {
                "claim_control_number": raw[2:25].strip(),
                "record_type": raw[0:2].strip(),
                "claim_status": raw[52:53].strip(),
                "service_count": str(service_count),
                "fixed_width_mir": "1",
            }

    # Legacy client P7A files are fixed-width reports rather than MIR-layout
    # records. Preserve support for them, but only when the row contains a
    # complete 23-character MIR reconciliation key. This is deliberately more
    # restrictive than the former fallback: it never creates ROW-* identifiers
    # and never associates a partial identifier with a claim.
    compact_raw = _claim_key(raw)
    candidates = re.findall(
        r"(?<![A-Za-z0-9])\d{17}[A-Za-z0-9]{6}(?![A-Za-z0-9])",
        raw,
    )
    claim_id = candidates[0] if candidates else ""
    if not claim_id:
        known = sorted(
            (_claim_key(value) for value in (known_claim_ids or []) if value),
            key=len,
            reverse=True,
        )
        claim_id = next(
            (value for value in known if len(value) >= 23 and value in compact_raw),
            "",
        )
    if claim_id:
        amounts = re.findall(
            r"(?<![A-Za-z0-9])(?:\(?[-+]?\$?\d[\d,]*\.\d{2}\)?)(?!\d)",
            raw,
        )
        if amounts:
            return {
                "claim_control_number": claim_id,
                "paid_amount": amounts[-1],
                "fixed_width_legacy_p7a": "1",
            }

    # Do not infer identifiers or paid amounts from arbitrary text. A row that
    # matches neither supported layout is retained as a parsing finding.
    return None


def _integer(value: str, default: int = 0) -> int:
    try:
        return int((value or "").strip())
    except (TypeError, ValueError):
        return default


def _mir_fixed_width_rows(raw: str, row_number: int, data: dict) -> list[dict]:
    """Expand one MIR-layout physical record into its exact service amounts."""
    if data.get("fixed_width_mir") != "1":
        return [{"row_number": row_number, "raw": raw, "data": data,
                 "segment_data": {"raw_fixed_width": raw}}]
    count = _integer(data.get("service_count"))
    if not count:
        return [{"row_number": row_number, "raw": raw, "data": data,
                 "segment_data": {"raw_fixed_width": raw}}]
    output = []
    for index in range(count):
        block = raw[334 + index * 303:334 + (index + 1) * 303]
        service_data = dict(data)
        service_data.update({
            "service_line_number": str(index + 1),
            "charge_amount": block[50:61],
            "allowed_amount": block[83:94],
            "paid_amount": block[94:105],
            "patient_responsibility": block[105:116],
        })
        output.append({
            "row_number": row_number,
            "raw": raw,
            "data": service_data,
            "segment_data": {"raw_fixed_width": raw, "service_block": block},
        })
    return output


def parse_recon_rows(text: str, known_claim_ids=None, include_findings=False):
    # Production files may end with the DOS EOF marker (SUB/0x1A). It is file
    # framing, not a financial record, and Python's str.strip() does not remove it.
    text = text.replace("\x1a", "")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("The RECON file is empty.")

    delimiter = _detect_delimiter(text)
    if not delimiter:
        output = []
        findings = []
        for number, raw in enumerate(lines, start=1):
            data = _fixed_width_data(raw, number, known_claim_ids)
            if data is None:
                findings.append({
                    "row_number": number,
                    "claim_control_number": "",
                    "error_code": "UNRECOGNIZED_RECON_LAYOUT",
                    "error_message": "Row does not match the supported fixed-width RECON layout; no values were inferred.",
                    "raw_record": raw,
                })
                continue
            output.extend(_mir_fixed_width_rows(raw, number, data))
        return (output, findings) if include_findings else output

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    parsed = list(reader)
    if not parsed:
        raise ValueError("The RECON file has no readable records.")
    header = [str(value).strip() or f"column_{index + 1}" for index, value in enumerate(parsed[0])]
    recognized = {_key(value) for value in header}
    known = set().union(*ALIASES.values())
    has_header = bool(recognized & known)
    data_rows = parsed[1:] if has_header else parsed
    if not has_header:
        header = [f"column_{index + 1}" for index in range(max(len(row) for row in data_rows))]

    output = []
    findings = []
    starting_row = 2 if has_header else 1
    for row_number, values in enumerate(data_rows, start=starting_row):
        if not any(str(value).strip() for value in values):
            continue
        original = delimiter.join(values)
        mapped = {header[index]: values[index] if index < len(values) else "" for index in range(len(header))}
        canonical = _canonical_row(mapped)
        if not canonical["claim_control_number"]:
            findings.append({
                "row_number": row_number,
                "claim_control_number": "",
                "error_code": "MISSING_CLAIM_IDENTIFIER",
                "error_message": "RECON row has no claim identifier; no synthetic identifier was created.",
                "raw_record": original,
            })
            continue
        invalid_money = next((
            field for field in (
                "charge_amount", "allowed_amount", "paid_amount",
                "patient_responsibility", "adjustment_amount",
                "mir904_bluecard_fee", "mir905_aea", "mir907_amount",
                "mir908_amount", "mpl920_pca_fee",
            )
            if not _valid_money(canonical.get(field))
        ), None)
        if invalid_money:
            findings.append({
                "row_number": row_number,
                "claim_control_number": canonical["claim_control_number"],
                "error_code": "INVALID_MONEY_VALUE",
                "error_message": f"RECON field {invalid_money} is not a valid monetary value; the row was held.",
                "raw_record": original,
            })
            continue
        output.append({
            "row_number": row_number,
            "raw": original,
            "data": canonical,
            "segment_data": mapped,
        })
    return (output, findings) if include_findings else output


@transaction.atomic
def process_recon_file(recon_file: RECONFile, actor=None) -> RECONProcessingRun:
    run = RECONProcessingRun.objects.create(
        recon_file=recon_file,
        client=recon_file.client,
        started_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    recon_file.status = "PROCESSING"
    recon_file.processing_started_at = timezone.now()
    recon_file.processing_error = ""
    recon_file.save(update_fields=["status", "processing_started_at", "processing_error", "updated_at"])

    try:
        known_claim_ids = list(
            MIRClaim.objects.filter(mir_file__client=recon_file.client)
            .exclude(claim_control_number="")
            .values_list("claim_control_number", flat=True)
        )
        rows, parsing_findings = parse_recon_rows(
            recon_file.file_content, known_claim_ids, include_findings=True
        )
        recon_file.claims.all().delete()
        grouped: OrderedDict[str, list[dict]] = OrderedDict()
        for row in rows:
            grouped.setdefault(row["data"]["claim_control_number"], []).append(row)

        total_charge = Decimal("0")
        total_paid = Decimal("0")
        service_total = 0
        for claim_sequence, (claim_number, claim_rows) in enumerate(grouped.items(), start=1):
            first = claim_rows[0]["data"]
            def first_money(field):
                value = next((
                    row["data"].get(field) for row in claim_rows
                    if str(row["data"].get(field) or "").strip()
                ), "")
                return _money_decimal(value)

            charge = sum((_money_decimal(row["data"].get("charge_amount")) for row in claim_rows), Decimal("0"))
            allowed = sum((_money_decimal(row["data"].get("allowed_amount")) for row in claim_rows), Decimal("0"))
            paid = sum((_money_decimal(row["data"].get("paid_amount")) for row in claim_rows), Decimal("0"))
            # MPL fee fields are claim-level values and may be repeated on
            # service rows. Take the first populated value, rather than
            # multiplying a fee by the number of services.
            mir904 = first_money("mir904_bluecard_fee")
            mir905 = first_money("mir905_aea")
            mir907 = first_money("mir907_amount")
            mir908 = first_money("mir908_amount")
            mpl920 = first_money("mpl920_pca_fee")
            patient = sum((_money_decimal(row["data"].get("patient_responsibility")) for row in claim_rows), Decimal("0"))
            adjustment = sum((_money_decimal(row["data"].get("adjustment_amount")) for row in claim_rows), Decimal("0"))
            claim = RECONClaim.objects.create(
                recon_file=recon_file,
                client=recon_file.client,
                claim_sequence=claim_sequence,
                claim_control_number=claim_number,
                member_id=first.get("member_id", ""),
                patient_control_number=first.get("patient_control_number", ""),
                record_type=first.get("record_type", ""),
                claim_status=first.get("claim_status", ""),
                service_count=len(claim_rows),
                charge_amount=charge,
                allowed_amount=allowed,
                paid_amount=paid,
                mir904_bluecard_fee=mir904,
                mir905_aea=mir905,
                mir907_amount=mir907,
                mir908_amount=mir908,
                mpl920_pca_fee=mpl920,
                patient_responsibility=patient,
                adjustment_amount=adjustment,
                service_from_date=first.get("service_from_date", "")[:10],
                service_to_date=claim_rows[-1]["data"].get("service_to_date", "")[:10],
                raw_record="\n".join(row["raw"] for row in claim_rows),
                segment_data={"rows": [row["segment_data"] for row in claim_rows]},
            )
            services = []
            for service_sequence, row in enumerate(claim_rows, start=1):
                data = row["data"]
                services.append(RECONServiceLine(
                    recon_claim=claim,
                    recon_file=recon_file,
                    service_sequence=service_sequence,
                    source_row_number=row["row_number"],
                    service_line_number=data.get("service_line_number", ""),
                    procedure_code=data.get("procedure_code", ""),
                    revenue_code=data.get("revenue_code", ""),
                    service_from_date=data.get("service_from_date", "")[:10],
                    service_to_date=data.get("service_to_date", "")[:10],
                    units=_decimal(data.get("units")),
                    charge_amount=_money_decimal(data.get("charge_amount")),
                    allowed_amount=_money_decimal(data.get("allowed_amount")),
                    paid_amount=_money_decimal(data.get("paid_amount")),
                    patient_responsibility=_money_decimal(data.get("patient_responsibility")),
                    adjustment_amount=_money_decimal(data.get("adjustment_amount")),
                    reason_code=data.get("reason_code", "")[:30],
                    raw_service=row["raw"],
                    segment_data=row["segment_data"],
                ))
            RECONServiceLine.objects.bulk_create(services, batch_size=1000)
            service_total += len(services)
            total_charge += charge
            total_paid += paid

        now = timezone.now()
        recon_file.status = (
            "FAILED" if parsing_findings and not rows
            else "PARTIAL" if parsing_findings
            else "PROCESSED"
        )
        recon_file.record_count = len(rows)
        recon_file.claim_count = len(grouped)
        recon_file.service_count = service_total
        recon_file.held_record_count = len(parsing_findings)
        recon_file.parsing_findings = parsing_findings
        recon_file.processing_error = (
            f"{len(parsing_findings)} RECON record(s) held for review."
            if parsing_findings else ""
        )
        recon_file.total_charge_amount = total_charge
        recon_file.total_paid_amount = total_paid
        recon_file.processed_at = now
        recon_file.save()
        run.status = (
            "FAILED" if parsing_findings and not rows
            else "PARTIAL" if parsing_findings
            else "COMPLETED"
        )
        run.claims_created = len(grouped)
        run.services_created = service_total
        run.invalid_records = len(parsing_findings)
        run.completed_at = now
        run.save()
        RECONProcessingError.objects.bulk_create([
            RECONProcessingError(processing_run=run, recon_file=recon_file, **finding)
            for finding in parsing_findings
        ])
        return run
    except Exception as exc:
        now = timezone.now()
        recon_file.status = "FAILED"
        recon_file.processing_error = str(exc)
        recon_file.processed_at = now
        recon_file.save(update_fields=["status", "processing_error", "processed_at", "updated_at"])
        run.status = "FAILED"
        run.error_message = str(exc)
        run.invalid_records = 1
        run.completed_at = now
        run.save()
        RECONProcessingError.objects.create(
            processing_run=run,
            recon_file=recon_file,
            row_number=0,
            error_code="PROCESSING_FAILED",
            error_message=str(exc),
        )
        raise


def ingest_sftp_recon_file(*, client, actor, filename, remote_path, raw, text):
    """Persist and process a RECON fetched from the configured SFTP folder.

    This intentionally uses ``process_recon_file`` so SFTP and manual uploads
    produce the same normalized claims, services, totals, and Result-page data.
    """
    file_hash = hashlib.sha256(raw).hexdigest()
    recon = RECONFile.objects.filter(client=client, file_hash=file_hash).first()
    already_exists = recon is not None

    if recon is None:
        recon = RECONFile.objects.create(
            client=client,
            uploaded_by=actor if getattr(actor, "is_authenticated", False) else None,
            original_filename=filename[:255],
            stored_filename=(
                f"{getattr(client, 'client_code', 'GLOBAL')}_{uuid.uuid4()}_{filename}"
            )[:255],
            file_content=text,
            file_hash=file_hash,
            file_size=len(raw),
            import_mode="SFTP",
        )
    else:
        # The latest ingestion source is SFTP. This also ensures a file first
        # uploaded manually is shown as SFTP after the Test pipeline fetches it.
        updates = []
        if recon.import_mode != "SFTP":
            recon.import_mode = "SFTP"
            updates.append("import_mode")
        if not recon.file_content:
            recon.file_content = text
            updates.append("file_content")
        if updates:
            updates.append("updated_at")
            recon.save(update_fields=updates)

    if recon.status != "PROCESSED":
        process_recon_file(recon, actor)
        recon.refresh_from_db()

    return {
        "already_exists": already_exists,
        "file": {
            "id": str(recon.id),
            "original_filename": recon.original_filename,
            "status": recon.status,
            "import_mode": "SFTP",
            "remote_path": remote_path,
            "claim_count": recon.claim_count,
            "processing_error": recon.processing_error,
        },
    }


def _x12_elements(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("The 837 file is empty.")
    # ISA is fixed width; element separator is character 4 and segment separator
    # is character 106 when present. Fall back to common separators.
    element_sep = text[3] if text.startswith("ISA") and len(text) > 3 else "*"
    segment_sep = text[105] if text.startswith("ISA") and len(text) > 105 else "~"
    return [seg.strip().split(element_sep) for seg in text.split(segment_sep) if seg.strip()]


def parse_837_rows(text):
    """Extract professional, institutional, and dental 837 claim services."""
    segments = _x12_elements(text)
    rows, current = [], None

    def procedure_code(composite):
        parts = [part.strip() for part in str(composite or "").split(":")]
        # Composite medical-procedure identifiers normally start with a
        # qualifier (HC/AD/etc.) followed by the actual procedure code.
        return parts[1] if len(parts) > 1 else (parts[0] if parts else "")

    def append_service(service_type, seg, procedure_index, charge_index,
                       units_index, revenue_index=None):
        procedure = procedure_code(seg[procedure_index] if len(seg) > procedure_index else "")
        charge = _decimal(seg[charge_index] if len(seg) > charge_index else "")
        units = _decimal(seg[units_index] if len(seg) > units_index else "")
        revenue = (seg[revenue_index] if revenue_index is not None and len(seg) > revenue_index else "").strip()
        current["services"].append({
            "service_type": service_type,
            "procedure_code": procedure,
            "revenue_code": revenue,
            "charge_amount": charge,
            "paid_amount": Decimal("0"),
            "allowed_amount": Decimal("0"),
            "units": units,
            "segment_data": {service_type: seg},
        })
        current["service_count"] += 1

    for seg in segments:
        tag = seg[0].strip().upper() if seg else ""
        if tag == "CLM":
            if current:
                rows.append(current)
            current = {
                "claim_control_number": (seg[1] if len(seg) > 1 else "").strip(),
                "member_id": "",
                "patient_control_number": (seg[1] if len(seg) > 1 else "").strip(),
                "claim_status": "",
                "service_count": 0,
                "charge_amount": _decimal(seg[2] if len(seg) > 2 else ""),
                "paid_amount": Decimal("0"),
                "services": [],
                "segment_data": {"CLM": seg},
            }
        elif current and tag == "NM1":
            entity = seg[1] if len(seg) > 1 else ""
            if entity in {"IL", "QC"}:
                current["member_id"] = (seg[9] if len(seg) > 9 else "").strip() or current["member_id"]
        elif current and tag == "SV1":
            append_service("SV1", seg, procedure_index=1, charge_index=2, units_index=4)
        elif current and tag == "SV2":
            append_service(
                "SV2", seg, procedure_index=2, charge_index=3,
                units_index=5, revenue_index=1,
            )
        elif current and tag == "SV3":
            append_service("SV3", seg, procedure_index=1, charge_index=2, units_index=6)
        elif current and tag == "DTP":
            current["segment_data"].setdefault("DTP", []).append(seg)
    if current:
        rows.append(current)
    if not rows:
        raise ValueError("No CLM claim segments were found in the 837 file.")
    claims_without_services = [
        row["claim_control_number"] or "(blank CLM01)"
        for row in rows if not row["services"]
    ]
    if claims_without_services:
        raise ValueError(
            "837 claim(s) contain no supported SV1, SV2, or SV3 service segments: "
            + ", ".join(claims_without_services)
        )
    return rows


@transaction.atomic
def ingest_837_reference(client, actor, filename, remote_path, raw, text):
    import hashlib, os, uuid
    file_hash = hashlib.sha256(raw).hexdigest()
    existing = RECONFile.objects.filter(client=client, file_hash=file_hash).first()
    if existing:
        return {"already_exists": True, "file": {"id": str(existing.id), "original_filename": existing.original_filename,
                "status": existing.status, "source": "SFTP", "remote_path": remote_path}}
    rows = parse_837_rows(text)
    recon = RECONFile.objects.create(
        client=client, uploaded_by=actor if getattr(actor, "is_authenticated", False) else None,
        original_filename=os.path.basename(filename)[:255],
        stored_filename=f"{getattr(client, 'client_code', 'GLOBAL')}_{uuid.uuid4()}_{os.path.basename(filename)}"[:255],
        file_content=text, file_hash=file_hash, file_size=len(raw), import_mode="SFTP", status="PROCESSING",
        processing_started_at=timezone.now(), processing_error="",
    )
    total_charge = Decimal("0")
    services_total = 0
    for sequence, data in enumerate(rows, start=1):
        services = data["services"]
        claim = RECONClaim.objects.create(
            recon_file=recon, client=client, claim_sequence=sequence,
            claim_control_number=data["claim_control_number"], member_id=data["member_id"],
            patient_control_number=data["patient_control_number"], record_type="837",
            claim_status=data["claim_status"], service_count=len(services),
            charge_amount=sum((s["charge_amount"] for s in services), Decimal("0")),
            allowed_amount=Decimal("0"), paid_amount=Decimal("0"),
            raw_record="", segment_data={**data["segment_data"], "source": "SFTP", "remote_path": remote_path},
        )
        RECONServiceLine.objects.bulk_create([
            RECONServiceLine(recon_claim=claim, recon_file=recon, service_sequence=i,
                source_row_number=0, procedure_code=s["procedure_code"],
                revenue_code=s.get("revenue_code", ""), units=s["units"],
                charge_amount=s["charge_amount"], allowed_amount=s["allowed_amount"],
                paid_amount=s["paid_amount"], raw_service="",
                segment_data={"source": "837", **s.get("segment_data", {})})
            for i, s in enumerate(services, start=1)
        ])
        total_charge += claim.charge_amount
        services_total += len(services)
    now = timezone.now()
    recon.status = "PROCESSED"
    recon.record_count = len(rows)
    recon.claim_count = len(rows)
    recon.service_count = services_total
    recon.total_charge_amount = total_charge
    recon.total_paid_amount = Decimal("0")
    recon.processed_at = now
    recon.save()
    return {"already_exists": False, "file": {"id": str(recon.id), "original_filename": recon.original_filename,
            "status": recon.status, "source": "SFTP", "remote_path": remote_path, "claim_count": recon.claim_count}}
