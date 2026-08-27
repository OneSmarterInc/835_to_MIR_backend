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


def reconciliation_rows(client, recon_file=None, page=None, page_size=200, claim_id=None):
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
    total = claims.count()
    if page is not None:
        start = (page - 1) * page_size
        claims = claims[start:start + page_size]
    claims = list(claims)

    # Only retain RECON rows needed by this page. A large RECON file must not
    # be materialized in a Gunicorn worker just to render a bounded page.
    wanted_claim_ids = {
        normalize_claim_id(claim.claim_control_number) for claim in claims
    }
    recon_by_claim = {}
    if recon_file and wanted_claim_ids:
        for recon_claim in recon_file.claims.only(
            "id", "claim_control_number", "paid_amount", "charge_amount", "service_count"
        ).iterator(chunk_size=2000):
            normalized_id = normalize_claim_id(recon_claim.claim_control_number)
            if normalized_id in wanted_claim_ids:
                recon_by_claim.setdefault(normalized_id, recon_claim)
    output = []
    for claim in claims:
        claim_number = normalize_claim_id(claim.claim_control_number)
        recon_claim = recon_by_claim.get(claim_number)
        recon_paid = _money(recon_claim.paid_amount if recon_claim else ZERO)
        status, remaining = reconciliation_status(claim.mir_payable, recon_paid, bool(recon_claim))
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
            "recon_claim_id": recon_claim.id if recon_claim else None,
            "recon_filename": recon_file.original_filename if recon_file else "",
            "recon_date": recon_file.processed_at.isoformat() if recon_file and recon_file.processed_at else None,
            "recon_service_count": recon_claim.service_count if recon_claim else 0,
            "recon_charge_amount": str(recon_claim.charge_amount) if recon_claim else "0.00",
            "recon_paid_amount": str(recon_paid),
            "remaining_amount": str(remaining),
            "difference_amount": str(remaining),
            "status": status,
        })
    return (output, total) if page is not None else output
