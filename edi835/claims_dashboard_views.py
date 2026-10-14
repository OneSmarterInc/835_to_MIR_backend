"""Read-only four-stream claims dashboard. Tenant-scope every query."""
from datetime import date

from django.core.paginator import Paginator
from django.http import JsonResponse
from django.utils import timezone
from project835.decorators import authenticated_api_required, json_api_errors
from admin_panel.access_control import can_access_client, scope_client_queryset

from .models import EDI835File, EDI835Claim, MIRFile, MIRClaim, EDI837File, EDI837Claim, RECONFile, RECONClaim
from .claim_numbers import split_claim_number

SOURCES = {
    "835": (EDI835File, "uploaded_at"),
    "MIR": (MIRFile, "converted_at"),
    "837": (EDI837File, "uploaded_at"),
    "RECON": (RECONFile, "uploaded_at"),
}


def _authorized_queryset(request, model):
    client_id = str(request.GET.get("client_id") or "").strip()
    queryset = scope_client_queryset(model.objects.all(), request.user)
    if client_id:
        if not can_access_client(request.user, client_id):
            return None
        queryset = queryset.filter(client_id=client_id)
    return queryset


def _json_date(request):
    value = str(request.GET.get("date") or timezone.localdate().isoformat())
    return date.fromisoformat(value)


def _file_data(kind, item):
    if kind == "835":
        name, count, held = item.original_filename, item.claims_count, item.held_claims_count
    elif kind == "MIR":
        name, count, held = item.mir_filename, item.claim_count, item.source_835.held_claims_count
    elif kind == "837":
        name, count, held = item.original_filename, item.claim_count, 0
    else:
        name, count, held = item.original_filename, item.claim_count, item.held_record_count
    return {"id": str(item.id), "name": name, "claim_count": count, "held_count": held,
            "status": item.status, "type": kind}


@authenticated_api_required
@json_api_errors
def dashboard_files(request):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "GET only"}, status=405)
    try:
        selected = _json_date(request)
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid ISO date"}, status=400)
    result = {}
    for kind, (model, timestamp) in SOURCES.items():
        qs = _authorized_queryset(request, model)
        if qs is None:
            return JsonResponse({"success": False, "error": "Access denied"}, status=403)
        qs = qs.filter(**{f"{timestamp}__date": selected}).order_by(f"-{timestamp}")
        if kind == "MIR":
            qs = qs.select_related("source_835")
        result[kind] = {"count": qs.count(), "files": [_file_data(kind, item) for item in qs[:500]],
                        "truncated": qs.count() > 500}
    return JsonResponse({"success": True, "date": selected.isoformat(), "groups": result})


@authenticated_api_required
@json_api_errors
def dashboard_file_claims(request, kind, file_id):
    kind = kind.upper()
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "GET only"}, status=405)
    if kind not in SOURCES:
        return JsonResponse({"success": False, "error": "Unsupported file type"}, status=404)
    files = _authorized_queryset(request, SOURCES[kind][0])
    if files is None:
        return JsonResponse({"success": False, "error": "Access denied"}, status=403)
    item = files.filter(id=file_id).first()
    if item is None:
        return JsonResponse({"success": False, "error": "File not found"}, status=404)
    if kind == "835":
        qs = EDI835Claim.objects.filter(edi_file=item)
    elif kind == "MIR":
        qs = MIRClaim.objects.filter(mir_file=item)
    elif kind == "837":
        qs = EDI837Claim.objects.filter(edi_file=item)
    else:
        qs = RECONClaim.objects.filter(recon_file=item)
    try:
        page_size = min(200, max(1, int(request.GET.get("page_size", 50))))
        page_number = max(1, int(request.GET.get("page", 1)))
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid pagination"}, status=400)
    pagination = Paginator(qs.order_by("claim_sequence"), page_size)
    page = pagination.get_page(page_number)
    claims = []
    for claim in page.object_list:
        identifier = (getattr(claim, "claim_control_number", None)
                      or getattr(claim, "highmark_claim_number", None) or "")
        if kind == "835":
            external, internal = claim.highmark_claim_number, claim.internal_claim_number
        elif kind == "837":
            external, internal = claim.highmark_claim_number or identifier, claim.internal_claim_number
        else:
            parts = split_claim_number(identifier)
            external, internal = parts.get("highmark_claim_number", ""), parts.get("internal_claim_number", "")
        claims.append({
            "claim_sequence": claim.claim_sequence,
            "highmark_claim_number": external, "internal_claim_number": internal,
            "member_id": getattr(claim, "member_id", ""),
            "patient_name": " ".join(filter(None, [getattr(claim, "patient_first_name", ""),
                                                   getattr(claim, "patient_last_name", "")])),
            "claim_status": getattr(claim, "claim_status", ""),
            "service_count": claim.service_count,
            "paid_amount": str(getattr(claim, "paid_amount", "")),
            "total_charge_amount": str(getattr(claim, "total_charge_amount", getattr(claim, "charge_amount", ""))),
        })
    held = []
    if kind == "MIR":
        for finding in (item.source_835.conversion_findings or []):
            if str(finding.get("severity", "")).upper() not in ("HOLD", "REFUSE"):
                continue
            parts = split_claim_number(finding.get("claim_control_number") or finding.get("claim_number") or "")
            held.append({"highmark_claim_number": parts.get("highmark_claim_number", ""),
                         "internal_claim_number": parts.get("internal_claim_number", ""),
                         "rule": finding.get("rule_code", ""),
                         "reason": finding.get("reason") or finding.get("detail") or "Claim requires review"})
    return JsonResponse({"success": True, "file": _file_data(kind, item),
                         "claims": claims, "held_claims": held,
                         "count": pagination.count, "page": page.number, "pages": pagination.num_pages})
