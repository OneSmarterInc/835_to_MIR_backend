"""RECON upload parsing and normalized database persistence."""

from __future__ import annotations

import csv
import io
import re
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


def _fixed_width_data(raw: str, row_number: int, known_claim_ids=None) -> dict:
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

    # For a client-specific fixed-width row, recognize an explicit full MIR
    # key. Never replace it with a partial known MIR identifier.
    compact_raw = _claim_key(raw)
    candidates = re.findall(r"(?<![A-Za-z0-9])\d{17}[A-Za-z0-9]{6}(?![A-Za-z0-9])", raw)
    claim_id = candidates[0] if candidates else ""
    if not claim_id:
        known = sorted((_claim_key(value) for value in (known_claim_ids or []) if value), key=len, reverse=True)
        claim_id = next((value for value in known if len(value) >= 23 and value in compact_raw), "")
    if not claim_id:
        claim_id = f"ROW-{row_number}"
    amounts = re.findall(r"(?<![A-Za-z0-9])(?:\(?[-+]?\$?\d[\d,]*\.\d{2}\)?)(?!\d)", raw)
    return {"claim_control_number": claim_id, "paid_amount": amounts[-1] if amounts else ""}


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


def parse_recon_rows(text: str, known_claim_ids=None) -> list[dict]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("The RECON file is empty.")

    delimiter = _detect_delimiter(text)
    if not delimiter:
        output = []
        for number, raw in enumerate(lines, start=1):
            data = _fixed_width_data(raw, number, known_claim_ids)
            output.extend(_mir_fixed_width_rows(raw, number, data))
        return output

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
    starting_row = 2 if has_header else 1
    for row_number, values in enumerate(data_rows, start=starting_row):
        if not any(str(value).strip() for value in values):
            continue
        original = delimiter.join(values)
        mapped = {header[index]: values[index] if index < len(values) else "" for index in range(len(header))}
        canonical = _canonical_row(mapped)
        if not canonical["claim_control_number"]:
            canonical["claim_control_number"] = f"ROW-{row_number}"
        output.append({
            "row_number": row_number,
            "raw": original,
            "data": canonical,
            "segment_data": mapped,
        })
    return output


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
        rows = parse_recon_rows(recon_file.file_content, known_claim_ids)
        recon_file.claims.all().delete()
        grouped: OrderedDict[str, list[dict]] = OrderedDict()
        for row in rows:
            grouped.setdefault(row["data"]["claim_control_number"], []).append(row)

        total_charge = Decimal("0")
        total_paid = Decimal("0")
        service_total = 0
        for claim_sequence, (claim_number, claim_rows) in enumerate(grouped.items(), start=1):
            first = claim_rows[0]["data"]
            charge = sum((_money_decimal(row["data"].get("charge_amount")) for row in claim_rows), Decimal("0"))
            allowed = sum((_money_decimal(row["data"].get("allowed_amount")) for row in claim_rows), Decimal("0"))
            paid = sum((_money_decimal(row["data"].get("paid_amount")) for row in claim_rows), Decimal("0"))
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
        recon_file.status = "PROCESSED"
        recon_file.record_count = len(rows)
        recon_file.claim_count = len(grouped)
        recon_file.service_count = service_total
        recon_file.total_charge_amount = total_charge
        recon_file.total_paid_amount = total_paid
        recon_file.processed_at = now
        recon_file.save()
        run.status = "COMPLETED"
        run.claims_created = len(grouped)
        run.services_created = service_total
        run.completed_at = now
        run.save()
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
