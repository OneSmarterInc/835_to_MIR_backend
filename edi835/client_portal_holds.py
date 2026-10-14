"""Client-specific, paginated held-claim drilldown for the attention queue."""
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from project835.decorators import authenticated_api_required
from .client_portal_views import only_client_user, finding_for, as_claim
from .models import EDI835File, EDI835Claim


@authenticated_api_required
@require_GET
def held_file_claims(request, file_id):
    client, denied = only_client_user(request)
    if denied:
        return denied
    source = EDI835File.objects.filter(pk=file_id, client=client).first()
    if source is None:
        return JsonResponse({"success": False, "error": "Source file not found"}, status=404)
    try:
        page_number = max(1, int(request.GET.get("page", 1)))
        page_size = min(50, max(1, int(request.GET.get("page_size", 20))))
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid pagination"}, status=400)

    findings = [
        f for f in (source.conversion_findings or [])
        if isinstance(f, dict) and str(f.get("severity") or "").upper() in {"HOLD", "REFUSE"}
    ]
    # Evaluate each normalized claim against the conversion finding's exact
    # claim sequence/control number. Do not show clean claims as held.
    matched = []
    matched_indices = set()
    for claim in EDI835Claim.objects.filter(edi_file=source).order_by("claim_sequence").iterator(chunk_size=500):
        finding = finding_for(claim, findings)
        if finding is None:
            continue
        matched.append(as_claim(
            claim, str(finding.get("reason") or finding.get("detail") or
                       finding.get("rule_code") or "Conversion hold")
        ))
        matched_indices.add(id(finding))
    # A hold may originate in the generator before normalized claim rows
    # exist. Preserve the recorded finding rather than fabricate a claim id.
    for finding in findings:
        if id(finding) in matched_indices:
            continue
        matched.append({
            "id": None,
            "number": finding.get("claim_number") or finding.get("claim_control_number") or "",
            "internal_number": "",
            "billed": None, "paid": None, "patient_owes": None,
            "payer_status": "Conversion hold",
            "reason": finding.get("reason") or finding.get("detail") or finding.get("rule_code") or "Review required",
        })
    pagination = Paginator(matched, page_size)
    current = pagination.get_page(page_number)
    return JsonResponse({
        "success": True, "source": source.original_filename,
        "count": pagination.count, "page": current.number,
        "pages": pagination.num_pages,
        "held_total": int(source.held_claims_count or 0),
        "claims": list(current.object_list),
    })
