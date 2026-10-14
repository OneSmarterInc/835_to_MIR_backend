"""Read-only work queue for the assigned Client User tenant."""
from datetime import timedelta
from django.db.models import Sum
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from project835.decorators import authenticated_api_required
from .models import EDI835Claim, EDI835File, MIRFile, ClientPortalAction


def only_client_user(request):
    user = request.user
    if (not user.is_authenticated or user.is_staff
            or user.client_role != "CLIENT_USER" or not user.client_id):
        return None, JsonResponse({"success": False, "error": "Client User access required"}, status=403)
    return user.client, None


def as_claim(claim, reason="", days=0):
    return {
        "id": claim.pk, "number": claim.highmark_claim_number,
        "internal_number": claim.internal_claim_number,
        "billed": str(claim.total_charge_amount), "paid": str(claim.paid_amount),
        "patient_owes": str(claim.patient_responsibility),
        "payer_status": ("Denied" if claim.claim_status == "4"
                         else "Paid in full" if claim.paid_amount >= claim.total_charge_amount
                         else "Paid less than billed" if claim.paid_amount > 0
                         else "No payment"),
        "reason": reason, "days": days,
        "file_id": str(claim.edi_file_id),
    }


def finding_for(claim, findings):
    for f in findings:
        number = str(f.get("claim_number") or f.get("claim_control_number") or "")
        index = str(f.get("claim_index") or "")
        external = str(claim.highmark_claim_number or "").strip()
        internal = str(claim.internal_claim_number or "").strip()
        exact = bool(number and number in {external, internal})
        # Some MIR findings concatenate CLP01 and CLP07. Only use that
        # fallback for sufficiently specific CLP01 identifiers.
        full_control = bool(external and len(external) >= 6 and number.startswith(external)
                            and len(number) > len(external))
        if (index and index == str(claim.claim_sequence)) or exact or full_control:
            return f
    return None


@authenticated_api_required
@require_GET
def overview(request):
    client, denied = only_client_user(request)
    if denied:
        return denied
    today = timezone.localdate()
    try:
        from datetime import date
        selected = date.fromisoformat(request.GET.get("date", today.isoformat()))
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid date"}, status=400)
    incoming = EDI835File.objects.filter(client=client, uploaded_at__date=selected)
    converted = MIRFile.objects.filter(client=client, converted_at__date=selected)
    sent = MIRFile.objects.filter(client=client, status="PUSHED", updated_at__date=selected)
    cutoff = timezone.now() - timedelta(days=90)
    waiting_files = list(EDI835File.objects.filter(
        client=client, uploaded_at__gte=cutoff, held_claims_count__gt=0
    ).order_by("-uploaded_at")[:150])
    issues, claims = [], []
    for file in waiting_files:
        days = max(0, (today - timezone.localtime(file.uploaded_at).date()).days)
        findings = [f for f in file.conversion_findings or []
                    if isinstance(f, dict) and str(f.get("severity", "")).upper() in ("HOLD", "REFUSE")]
        first = findings[0] if findings else {}
        reason = first.get("reason") or first.get("detail") or first.get("rule_code") or "Conversion hold"
        issues.append({"id": str(file.pk), "kind": "hold", "filename": file.original_filename,
                       "count": file.held_claims_count, "reason": reason, "days": days})
        if len(claims) < 200 and findings:
            for claim in EDI835Claim.objects.filter(edi_file=file).order_by("claim_sequence")[:500]:
                match = finding_for(claim, findings)
                if match:
                    why = match.get("reason") or match.get("detail") or match.get("rule_code") or "Conversion hold"
                    claims.append(as_claim(claim, why, days))
                if len(claims) >= 200:
                    break
    for file in EDI835File.objects.filter(
        client=client, uploaded_at__gte=cutoff, status="ERROR"
    ).order_by("-uploaded_at")[:50]:
        issues.append({"id": str(file.pk), "kind": "error", "filename": file.original_filename,
                       "count": 0, "reason": "File processing failed; ask your Client Admin to review the source file.",
                       "days": max(0, (today - timezone.localtime(file.uploaded_at).date()).days)})
    issues.sort(key=lambda i: -i["days"])
    claims.sort(key=lambda c: -c["days"])
    issue_file_ids = {item["id"] for item in issues}
    latest_hidden = {}
    for event in ClientPortalAction.objects.filter(
        client=client, file_id__in=issue_file_ids,
        kind__in=("IGNORE_FILE", "REOPEN_FILE"),
    ).order_by("created_at", "pk").values("file_id", "kind"):
        latest_hidden[str(event["file_id"])] = event["kind"] == "IGNORE_FILE"
    history = [{"id": str(m.pk), "filename": m.mir_filename,
                "date": m.converted_at.isoformat(), "count": m.claim_count, "status": m.status,
                "source_filename": m.source_835.original_filename}
               for m in MIRFile.objects.filter(client=client).select_related("source_835").order_by("-converted_at")[:30]]
    return JsonResponse({
        "success": True, "client": client.name, "date": selected.isoformat(),
        "metrics": {
            "received_files": incoming.count(),
            "received_claims": incoming.aggregate(n=Sum("claims_count"))["n"] or 0,
            "converted_claims": converted.aggregate(n=Sum("claim_count"))["n"] or 0,
            "sent_files": sent.count(),
            "sent_claims": sent.aggregate(n=Sum("claim_count"))["n"] or 0,
            "waiting_claims": sum(i["count"] for i in issues if i["kind"] == "hold"),
            "oldest_days": max((i["days"] for i in issues if i["kind"] == "hold"), default=0),
            "decisions": len(issues),
        },
        "claims": claims, "claims_limited": len(claims) >= 200,
        "issues": issues, "ignored_files": [key for key, value in latest_hidden.items() if value],
        "history": history,
    })


@authenticated_api_required
@require_GET
def history(request):
    """Complete paginated conversion history, always limited to this tenant."""
    client, denied = only_client_user(request)
    if denied:
        return denied
    try:
        number = max(1, int(request.GET.get("page", "1")))
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid page"}, status=400)
    pages = Paginator(
        MIRFile.objects.filter(client=client).select_related("source_835").order_by("-converted_at"),
        40,
    )
    page = pages.get_page(number)
    return JsonResponse({
        "success": True, "page": page.number, "pages": pages.num_pages,
        "count": pages.count, "files": [
            {
                "id": str(item.pk),
                "filename": item.mir_filename,
                "source_filename": item.source_835.original_filename,
                "date": item.converted_at.isoformat(),
                "count": item.claim_count, "status": item.status,
            }
            for item in page.object_list
        ],
    })
