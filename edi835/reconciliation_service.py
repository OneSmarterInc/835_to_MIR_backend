"""Claim-level MIR to RECON reconciliation.

MIR continuation rows are already normalized into one MIRClaim with all service
lines attached, so every result here is one logical MIR claim, irrespective of
the 50-service physical-row limit.
"""

from decimal import Decimal
import re

from django.db.models import Sum
from django.db.models.functions import Coalesce

from .models import MIRClaim, RECONClaim, RECONFile


ZERO = Decimal("0.00")


def normalize_claim_id(value):
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def latest_recon_file(client, recon_file_id=None):
    files = RECONFile.objects.filter(client=client, status="PROCESSED")
    if recon_file_id:
        return files.filter(id=recon_file_id).first()
    return files.order_by("-processed_at", "-uploaded_at").first()


def _money(value):
    return value if value is not None else ZERO


def reconciliation_status(amount_to_pay, recon_paid, matched):
    amount_to_pay, recon_paid = _money(amount_to_pay), _money(recon_paid)
    remaining = amount_to_pay - recon_paid
    if not matched:
        return "NOT_IN_RECON", remaining
    if amount_to_pay and recon_paid and ((amount_to_pay < 0) != (recon_paid < 0)):
        return "SIGNATURE_MISMATCH", remaining
    if remaining == ZERO:
        return "CLEAR", remaining
    if recon_paid == ZERO:
        return "UNPAID", remaining
    if abs(recon_paid) < abs(amount_to_pay):
        return "PARTIALLY_PAID", remaining
    if abs(recon_paid) > abs(amount_to_pay):
        return "OVERPAID", remaining
    return "AMOUNT_MISMATCH", remaining


SORT_FIELDS = {
    "claim_id": lambda row: row["claim_id"].casefold(),
    "patient_name": lambda row: row["patient_name"].casefold(),
    "mir_filename": lambda row: row["mir_filename"].casefold(),
    "recon_filename": lambda row: row["recon_filename"].casefold(),
    "amount_to_pay": lambda row: Decimal(row["amount_to_pay"]),
    "recon_paid_amount": lambda row: Decimal(row["recon_paid_amount"]),
    "difference_amount": lambda row: Decimal(row["difference_amount"]),
    "status": lambda row: row["status"].casefold(),
}


def reconciliation_rows(
    client, recon_files=None, page=None, page_size=200, claim_id=None, search="",
    sort_by="", sort_direction="asc",
):
    claims = (
        MIRClaim.objects.filter(mir_file__client=client)
        .select_related("mir_file")
        .annotate(
            mir_charge=Coalesce(Sum("service_lines__charge_amount"), ZERO),
            mir_payable=Coalesce(Sum("service_lines__paid_amount"), ZERO),
        )
        .order_by("-mir_file__converted_at", "claim_sequence")
    )
    if claim_id is not None:
        claims = claims.filter(id=claim_id)
    claims = list(claims)

    recon_by_claim = {}
    recon_claims = RECONClaim.objects.filter(
        recon_file__in=(recon_files or []), recon_file__status="PROCESSED"
    ).select_related("recon_file").only(
        "id", "claim_control_number", "member_id", "patient_control_number",
        "paid_amount", "charge_amount", "service_count",
        "recon_file__id", "recon_file__original_filename", "recon_file__processed_at",
    ).order_by("recon_file__processed_at", "recon_file__uploaded_at", "claim_sequence")
    for recon_claim in recon_claims.iterator(chunk_size=2000):
        normalized_id = normalize_claim_id(recon_claim.claim_control_number)
        if normalized_id:
            recon_by_claim.setdefault(normalized_id, []).append(recon_claim)

    output = []
    mir_claim_ids = set()
    for claim in claims:
        claim_number = normalize_claim_id(claim.claim_control_number)
        mir_claim_ids.add(claim_number)
        matches = recon_by_claim.get(claim_number, [])
        recon_paid = sum((_money(item.paid_amount) for item in matches), ZERO)
        recon_charge = sum((_money(item.charge_amount) for item in matches), ZERO)
        recon_services = sum((item.service_count for item in matches), 0)
        matched_files = list(dict.fromkeys(item.recon_file.original_filename for item in matches))
        latest_match = matches[-1] if matches else None
        status, remaining = reconciliation_status(claim.mir_payable, recon_paid, bool(matches))
        recon_matches = [{
            "recon_claim_id": item.id,
            "filename": item.recon_file.original_filename,
            "date": item.recon_file.processed_at.isoformat() if item.recon_file.processed_at else None,
            "paid_amount": str(_money(item.paid_amount)),
            "charge_amount": str(_money(item.charge_amount)),
            "service_count": item.service_count,
        } for item in matches]
        output.append({
            "mir_claim_id": claim.id,
            "claim_id": claim_number,
            "patient_name": " ".join(part for part in [claim.patient_first_name, claim.patient_last_name] if part).strip(),
            "member_id": claim.member_id,
            "mir_filename": claim.mir_file.mir_filename,
            "mir_date": claim.mir_file.converted_at.isoformat(),
            "mir_service_count": claim.service_count,
            "mir_charge_amount": str(claim.mir_charge),
            "amount_to_pay": str(claim.mir_payable),
            "recon_claim_id": latest_match.id if latest_match else None,
            "recon_filename": ", ".join(matched_files),
            "recon_date": latest_match.recon_file.processed_at.isoformat() if latest_match and latest_match.recon_file.processed_at else None,
            "recon_service_count": recon_services,
            "recon_charge_amount": str(recon_charge),
            "recon_paid_amount": str(recon_paid),
            "recon_matches": recon_matches,
            "remaining_amount": str(remaining),
            "difference_amount": str(remaining),
            "status": status,
        })

    # RECON claims without a corresponding MIR record remain visible. Their
    # RECON occurrences are aggregated exactly like matched claims, while MIR
    # values remain empty and the status explains the missing side.
    if claim_id is None:
        for claim_number, matches in recon_by_claim.items():
            if claim_number in mir_claim_ids:
                continue
            recon_paid = sum((_money(item.paid_amount) for item in matches), ZERO)
            recon_charge = sum((_money(item.charge_amount) for item in matches), ZERO)
            recon_services = sum((item.service_count for item in matches), 0)
            latest_match = matches[-1]
            matched_files = list(dict.fromkeys(item.recon_file.original_filename for item in matches))
            recon_matches = [{
                "recon_claim_id": item.id,
                "filename": item.recon_file.original_filename,
                "date": item.recon_file.processed_at.isoformat() if item.recon_file.processed_at else None,
                "paid_amount": str(_money(item.paid_amount)),
                "charge_amount": str(_money(item.charge_amount)),
                "service_count": item.service_count,
            } for item in matches]
            output.append({
                "mir_claim_id": None,
                "claim_id": claim_number,
                "patient_name": "",
                "member_id": latest_match.member_id or latest_match.patient_control_number,
                "mir_filename": "",
                "mir_date": None,
                "mir_service_count": 0,
                "mir_charge_amount": str(ZERO),
                "amount_to_pay": str(ZERO),
                "recon_claim_id": latest_match.id,
                "recon_filename": ", ".join(matched_files),
                "recon_date": latest_match.recon_file.processed_at.isoformat() if latest_match.recon_file.processed_at else None,
                "recon_service_count": recon_services,
                "recon_charge_amount": str(recon_charge),
                "recon_paid_amount": str(recon_paid),
                "recon_matches": recon_matches,
                "remaining_amount": str(-recon_paid),
                "difference_amount": str(-recon_paid),
                "status": "NOT_IN_MIR",
            })

    search_value = (search or "").strip().casefold()
    if search_value:
        searchable_fields = (
            "claim_id", "patient_name", "member_id", "mir_filename",
            "recon_filename", "status",
        )
        output = [row for row in output if any(
            search_value in str(row.get(field) or "").casefold()
            for field in searchable_fields
        )]
    total = len(output)
    if sort_by in SORT_FIELDS:
        key = SORT_FIELDS[sort_by]
        output.sort(key=key, reverse=sort_direction == "desc")
    if page is not None:
        start = (page - 1) * page_size
        output = output[start:start + page_size]
    return (output, total) if page is not None else output
