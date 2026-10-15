"""Client-specific, paginated held-claim drilldown for the attention queue."""
import uuid
from itertools import groupby

from django.core.paginator import Paginator
from django.utils import timezone
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from project835.decorators import authenticated_api_required
from .client_portal_views import only_client_user, finding_for, as_claim
from .models import EDI835File, EDI835Claim
from .client_portal_activity import attach_claim_activity
from .client_portal_period import InvalidPeriod, resolve_period, apply_period, period_payload


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

    matched = held_claim_rows(source, EDI835Claim.objects.filter(
        edi_file=source
    ).order_by("claim_sequence").iterator(chunk_size=500))
    pagination = Paginator(matched, page_size)
    current = pagination.get_page(page_number)
    rows = list(current.object_list)
    attach_claim_activity(client, rows)
    return JsonResponse({
        "success": True, "source": source.original_filename,
        "count": pagination.count, "page": current.number,
        "pages": pagination.num_pages,
        "held_total": int(source.held_claims_count or 0), "claims": rows,
    })


def held_claim_rows(source, claims) -> list[dict]:
    days = max(0, (timezone.localdate() - timezone.localtime(source.uploaded_at).date()).days)
    findings = [
        f for f in (source.conversion_findings or [])
        if isinstance(f, dict) and str(f.get("severity") or "").upper() in {"HOLD", "REFUSE"}
    ]
    # Evaluate each normalized claim against the conversion finding's exact
    # claim sequence/control number. Do not show clean claims as held.
    matched = []
    matched_indices = set()
    for claim in claims:
        related = [
            (position, finding)
            for position, finding in enumerate(findings)
            if finding_for(claim, [finding]) is not None
        ]
        if not related:
            continue
        matched_indices.update(position for position, _ in related)
        explanations = list(dict.fromkeys(str(
            f.get("reason") or f.get("detail") or f.get("rule_code") or "Conversion hold"
        ) for _, f in related))
        matched.append(as_claim(claim, "; ".join(explanations), days))

    # Some conversion findings have no normalized CLP row yet. Group by
    # actual claim number or source sequence to avoid showing one claim
    # multiple times when it has multiple failed checks.
    unresolved = {}
    for position, finding in enumerate(findings):
        if position in matched_indices:
            continue
        claim_number = str(finding.get("claim_number") or finding.get("claim_control_number") or "")
        claim_index = str(finding.get("claim_index") or "")
        group_key = (claim_number, claim_index) if claim_number else ("index", claim_index)
        if not group_key[1] and not claim_number:
            group_key = ("finding", str(position))
        reason = str(finding.get("reason") or finding.get("detail") or
                     finding.get("rule_code") or "Review required")
        if group_key not in unresolved:
            unresolved[group_key] = {
                "id": None, "number": claim_number, "internal_number": "",
                "billed": None, "paid": None, "patient_owes": None,
                "payer_status": "Conversion hold",
                "reason": reason,
            }
        elif reason not in unresolved[group_key]["reason"]:
            unresolved[group_key]["reason"] += "; " + reason
    matched.extend(unresolved.values())
    for index, row in enumerate(matched):
        row.update(file_id=str(source.pk), file_name=source.original_filename,
                   days=days, row_key=str(source.pk) + ":" + str(row["id"] or "finding-" + str(index)))
    return matched


@authenticated_api_required
@require_GET
def attention_claims(request):
    """All held claims, not a capped file preview; page after matching holds."""
    client, denied = only_client_user(request)
    if denied:
        return denied
    try:
        number = max(1, int(request.GET.get("page", "1")))
        page_size = min(50, max(1, int(request.GET.get("page_size", "10"))))
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid pagination"}, status=400)
    try:
        period = resolve_period(request.GET, client, default="all")
    except InvalidPeriod as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)
    sources = apply_period(
        EDI835File.objects.filter(client=client, held_claims_count__gt=0),
        "uploaded_at", period,
    )
    if request.GET.get("file_id"):
        try:
            file_id = uuid.UUID(request.GET["file_id"])
        except ValueError:
            return JsonResponse({"success": False, "error": "Invalid source file"}, status=400)
        sources = sources.filter(pk=file_id)
    files = {source.pk: source for source in sources.only(
        "id", "original_filename", "uploaded_at", "conversion_findings", "held_claims_count"
    )}
    rows = []
    normalized = EDI835Claim.objects.filter(edi_file_id__in=files).only(
        "id", "edi_file_id", "claim_sequence", "highmark_claim_number",
        "internal_claim_number", "total_charge_amount", "paid_amount",
        "patient_responsibility", "claim_status",
    ).order_by("edi_file_id", "claim_sequence", "pk").iterator(chunk_size=1000)
    seen = set()
    for file_id, claims in groupby(normalized, key=lambda claim: claim.edi_file_id):
        rows.extend(held_claim_rows(files[file_id], claims))
        seen.add(file_id)
    for file_id in files.keys() - seen:
        rows.extend(held_claim_rows(files[file_id], []))
    rows.sort(key=lambda row: (-row["days"], row["file_id"], row["row_key"]))
    pagination = Paginator(rows, page_size)
    current = pagination.get_page(number)
    page_rows = list(current.object_list)
    attach_claim_activity(client, page_rows)
    return JsonResponse({
        "success": True, "claims": page_rows, "page_size": page_size,
        "period": period_payload(period),
        "count": pagination.count, "page": current.number,
        "pages": pagination.num_pages,
        "held_total": sum(source.held_claims_count for source in files.values()),
        "unmatched_count": sum(row["id"] is None for row in rows),
    })
