"""Claim-level MIR to RECON reconciliation.

MIR continuation rows are already normalized into one MIRClaim with all service
lines attached, so every result here is one logical MIR claim, irrespective of
the 50-service physical-row limit.
"""

from decimal import Decimal
import re

from django.db.models import Q, Sum
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


def reconciliation_rows(client, recon_files=None, page=None, page_size=200, claim_id=None, search=""):
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
    search = (search or "").strip()
    if search:
        claims = claims.filter(
            Q(claim_control_number__icontains=search)
            | Q(member_id__icontains=search)
            | Q(patient_first_name__icontains=search)
            | Q(patient_last_name__icontains=search)
            | Q(mir_file__mir_filename__icontains=search)
            | Q(mir_file__original_835_filename__icontains=search)
        )
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
    if wanted_claim_ids:
        recon_claims = RECONClaim.objects.filter(
            recon_file__in=(recon_files or []), recon_file__status="PROCESSED"
        ).select_related("recon_file").only(
            "id", "claim_control_number", "paid_amount", "charge_amount", "service_count",
            "recon_file__id", "recon_file__original_filename", "recon_file__processed_at",
        ).order_by("recon_file__processed_at", "recon_file__uploaded_at", "claim_sequence")
        for recon_claim in recon_claims.iterator(chunk_size=2000):
            normalized_id = normalize_claim_id(recon_claim.claim_control_number)
            if normalized_id in wanted_claim_ids:
                recon_by_claim.setdefault(normalized_id, []).append(recon_claim)
    output = []
    for claim in claims:
        claim_number = normalize_claim_id(claim.claim_control_number)
        matches = recon_by_claim.get(claim_number, [])
        recon_paid = sum((_money(item.paid_amount) for item in matches), ZERO)
        recon_charge = sum((_money(item.charge_amount) for item in matches), ZERO)
        recon_services = sum((item.service_count for item in matches), 0)
        matched_files = list(dict.fromkeys(item.recon_file.original_filename for item in matches))
        latest_match = matches[-1] if matches else None
        status, remaining = reconciliation_status(claim.mir_payable, recon_paid, bool(matches))
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
            "remaining_amount": str(remaining),
            "difference_amount": str(remaining),
            "status": status,
        })
    return (output, total) if page is not None else output
