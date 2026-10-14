"""Tenant-bound 835 claim detail for Client User accounts."""
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from project835.decorators import authenticated_api_required
from .models import EDI835Claim
from .client_portal_views import only_client_user, as_claim, finding_for


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
    file = claim.edi_file
    findings = [f for f in file.conversion_findings or []
                if isinstance(f, dict) and str(f.get("severity", "")).upper() in ("HOLD", "REFUSE")]
    match = finding_for(claim, findings)
    reason = ((match.get("reason") or match.get("detail") or
               match.get("rule_code") or "") if match else "")
    days = max(0, (timezone.localdate() - timezone.localtime(file.uploaded_at).date()).days) if match else 0
    services, adjustments = [], []
    for segment in (claim.segment_data or {}).get("segments", []):
        if not isinstance(segment, str):
            continue
        parts = segment.split("*")
        if parts[0] == "SVC":
            services.append({
                "code": parts[1] if len(parts) > 1 else "",
                "billed": parts[2] if len(parts) > 2 else "",
                "paid": parts[3] if len(parts) > 3 else "",
                "units": parts[5] if len(parts) > 5 else "",
            })
        elif parts[0] == "CAS":
            adjustments.append({
                "group": parts[1] if len(parts) > 1 else "",
                "code": parts[2] if len(parts) > 2 else "",
                "amount": parts[3] if len(parts) > 3 else "",
            })
    return JsonResponse({
        "success": True,
        "claim": {
            **as_claim(claim, reason, days),
            "status_code": claim.claim_status, "service_count": claim.service_count,
            "file_name": file.original_filename, "received_at": file.uploaded_at.isoformat(),
            "services": services, "adjustments": adjustments,
            "timeline": [
                {"label": "835 received", "at": file.uploaded_at.isoformat()},
                *([{"label": "Conversion completed", "at": file.processing_completed_at.isoformat()}]
                  if file.processing_completed_at else []),
            ],
        },
    })
