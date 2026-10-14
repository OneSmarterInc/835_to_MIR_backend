"""Tenant-bound payer interpretation from persisted 835 CLP/SVC/CAS segments."""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from project835.decorators import authenticated_api_required
from .models import EDI835Claim
from .client_portal_views import only_client_user, as_claim, finding_for


def amount(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def cas_items(fields):
    values = fields[2:]
    result = []
    for offset in range(0, len(values), 3):
        if offset + 1 >= len(values):
            break
        if not values[offset]:
            continue
        result.append({
            "group": fields[1] if len(fields) > 1 else "",
            "code": values[offset],
            "amount": values[offset + 1],
            "quantity": values[offset + 2] if offset + 2 < len(values) else "",
        })
    return result


def file_payment_header(source):
    # N1*PR payer, TRN*1 check/EFT reference and BPR payment date are
    # file-level 835 facts. Never manufacture these from the upload timestamp.
    header = (source or "").split("CLP*", 1)[0][:20000]
    result = {"payer": "", "check_number": "", "check_date": ""}
    for segment in re.split(r"[~\r\n]+", header):
        fields = segment.strip().split("*")
        if fields[0] == "N1" and len(fields) > 2 and fields[1] == "PR":
            result["payer"] = fields[2]
        elif fields[0] == "TRN" and len(fields) > 2:
            result["check_number"] = fields[2]
        elif fields[0] == "BPR" and len(fields) > 16 and re.fullmatch(r"\d{8}", fields[16]):
            result["check_date"] = fields[16]
    return result


@authenticated_api_required
@require_GET
def detail(request, claim_id):
    client, denied = only_client_user(request)
    if denied:
        return denied
    claim = EDI835Claim.objects.select_related("edi_file").filter(
        pk=claim_id, edi_file__client=client
    ).first()
    if not claim:
        return JsonResponse({"success": False, "error": "Claim not found"}, status=404)
    source_file = claim.edi_file
    findings = [finding for finding in (source_file.conversion_findings or [])
                if isinstance(finding, dict)
                and str(finding.get("severity", "")).upper() in ("HOLD", "REFUSE")]
    match = finding_for(claim, findings)
    reason = (match.get("reason") or match.get("detail") or
              match.get("rule_code") or "") if match else ""
    days = max(0, (timezone.localdate() -
                   timezone.localtime(source_file.uploaded_at).date()).days) if match else 0

    services, claim_adjustments = [], []
    current_service = None
    patient = ""
    for segment in (claim.segment_data or {}).get("segments") or []:
        if not isinstance(segment, str):
            continue
        fields = segment.strip().split("*")
        tag = fields[0].upper()
        if tag == "SVC":
            current_service = {
                "code": fields[1].split(":", 1)[-1] if len(fields) > 1 else "",
                "billed": fields[2] if len(fields) > 2 else "",
                "paid": fields[3] if len(fields) > 3 else "",
                "units": fields[5] if len(fields) > 5 else "",
                "date": "", "adjustments": [], "allowed": None,
            }
            services.append(current_service)
        elif tag == "CAS":
            entries = cas_items(fields)
            if current_service is None:
                claim_adjustments.extend(entries)
            else:
                current_service["adjustments"].extend(entries)
        elif tag == "DTM" and current_service is not None and len(fields) > 2 and fields[1] == "472":
            current_service["date"] = fields[2]
        elif tag == "NM1" and len(fields) > 4 and fields[1] == "QC":
            patient = " ".join([fields[4], fields[3]]).strip()
    for service in services:
        contractual = [
            amount(a["amount"]) for a in service["adjustments"]
            if a["group"] == "CO"
        ]
        if contractual:
            service["allowed"] = str(amount(service["billed"]) - sum(contractual))
    all_adjustments = claim_adjustments + [
        a for service in services for a in service["adjustments"]
    ]
    contractual = [amount(a["amount"]) for a in all_adjustments if a["group"] == "CO"]
    allowed = str(claim.total_charge_amount - sum(contractual)) if contractual else None
    payment = file_payment_header(source_file.input_file_content)
    mir = getattr(source_file, "mir_file", None)
    return JsonResponse({
        "success": True,
        "claim": {
            **as_claim(claim, reason, days),
            "status_code": claim.claim_status,
            "service_count": claim.service_count,
            "file_name": source_file.original_filename,
            "file_id": str(source_file.pk),
            "received_at": source_file.uploaded_at.isoformat(),
            "mir_status": mir.status if mir else "NOT_GENERATED",
            "patient_name": patient,
            "payer": payment["payer"],
            "check_number": payment["check_number"],
            "check_date": payment["check_date"],
            "allowed": allowed,
            "allowed_label": "Net after contractual (CO) adjustments" if allowed is not None else "Not reported",
            "services": services,
            "adjustments": all_adjustments,
            "reason_code": match.get("rule_code", "") if match else "",
            "remark_code": match.get("remark_code", "") if match else "",
            "technical_segments": (claim.segment_data or {}).get("segments", [])[:250],
            "timeline": [
                {"label": "835 received", "at": source_file.uploaded_at.isoformat()},
                *([{"label": "Conversion completed", "at": source_file.processing_completed_at.isoformat()}]
                  if source_file.processing_completed_at else []),
            ],
        },
    })
