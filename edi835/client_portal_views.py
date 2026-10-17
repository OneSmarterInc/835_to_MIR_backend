"""Read-only work queue for the assigned Client User tenant."""
from zoneinfo import ZoneInfo
from django.db.models import Sum
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from project835.decorators import authenticated_api_required
from .models import EDI835Claim, EDI835File, MIRFile, ClientPortalAction
from .client_portal_activity import attach_claim_activity
from .client_portal_financials import financial_metrics
from .client_portal_period import InvalidPeriod, resolve_period, apply_period, period_payload


def only_client_user(request, *, allowed_roles=("CLIENT_USER",)):
    user = request.user
    if (not user.is_authenticated or user.is_staff
            or user.client_role not in allowed_roles or not user.client_id):
        return None, JsonResponse({"success": False, "error": "Client User access required"}, status=403)
    return user.client, None


def as_claim(claim, reason="", days=0):
    return {
        "id": claim.pk, "number": claim.highmark_claim_number,
        "internal_number": claim.internal_claim_number,
        "billed": str(claim.total_charge_amount), "paid": str(claim.paid_amount),
        "patient_owes": str(claim.patient_responsibility),
        "responsibility_amount": str(claim.patient_responsibility),
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
    """Date-consistent client dashboard: all metrics and previews share one period."""
    client, denied = only_client_user(request)
    if denied:
        return denied
    try:
        period = resolve_period(request.GET, client)
    except InvalidPeriod as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)

    zone = ZoneInfo(period["timezone"])
    today = period["today"]
    inbound = apply_period(
        EDI835File.objects.filter(client=client), "uploaded_at", period
    )
    converted = apply_period(
        MIRFile.objects.filter(client=client), "converted_at", period
    )
    sent = apply_period(
        MIRFile.objects.filter(client=client, status="PUSHED"), "updated_at", period
    )
    # The 835 arrival period determines the held work queue. Financial streams
    # use their own event dates below, independently of this preview.
    held_qs = inbound.filter(held_claims_count__gt=0)
    errors_qs = inbound.filter(status="ERROR", held_claims_count=0)
    held_total = held_qs.aggregate(total=Sum("held_claims_count"))["total"] or 0
    held_file_total = held_qs.count()
    error_file_total = errors_qs.count()
    oldest = held_qs.order_by("uploaded_at").values_list("uploaded_at", flat=True).first()

    # Totals use every matching source file. Preview rows are intentionally
    # bounded; the separate Needs Attention endpoint pages all matched holds.
    file_preview = list(held_qs.order_by("uploaded_at", "pk")[:150])
    error_preview = list(errors_qs.order_by("uploaded_at", "pk")[:50])
    issues, claims = [], []
    for source in file_preview:
        days = max(0, (today - timezone.localtime(source.uploaded_at, zone).date()).days)
        findings = [f for f in (source.conversion_findings or [])
                    if isinstance(f, dict) and str(f.get("severity") or "").upper() in ("HOLD", "REFUSE")]
        first_finding = findings[0] if findings else {}
        why = first_finding.get("reason") or first_finding.get("detail") or first_finding.get("rule_code") or "Conversion hold"
        issues.append({
            "id": str(source.pk), "kind": "hold", "filename": source.original_filename,
            "count": source.held_claims_count, "reason": why, "days": days,
        })
        if len(claims) >= 200 or not findings:
            continue
        for claim in EDI835Claim.objects.filter(edi_file=source).order_by("claim_sequence")[:500]:
            finding = finding_for(claim, findings)
            if finding:
                reason = finding.get("reason") or finding.get("detail") or finding.get("rule_code") or "Conversion hold"
                claims.append(as_claim(claim, reason, days))
            if len(claims) >= 200:
                break
    for source in error_preview:
        issues.append({
            "id": str(source.pk), "kind": "error", "filename": source.original_filename,
            "count": 0,
            "reason": "File processing failed; ask your Client Admin to review the source file.",
            "days": max(0, (today - timezone.localtime(source.uploaded_at, zone).date()).days),
        })
    issues.sort(key=lambda row: (-row["days"], row["id"]))
    claims.sort(key=lambda row: (-row["days"], row["file_id"], str(row["id"])))
    attach_claim_activity(client, claims)

    visible_ids = {row["id"] for row in issues}
    latest_hidden = {}
    for action in ClientPortalAction.objects.filter(
        client=client, file_id__in=visible_ids,
        kind__in=("IGNORE_FILE", "REOPEN_FILE"),
    ).order_by("created_at", "pk").values("file_id", "kind"):
        latest_hidden[str(action["file_id"])] = action["kind"] == "IGNORE_FILE"

    money = financial_metrics(client, period)
    preview_history = [
        {"id": str(m.pk), "filename": m.mir_filename,
         "date": m.converted_at.isoformat(), "count": m.claim_count,
         "status": m.status, "source_filename": m.source_835.original_filename}
        for m in converted.select_related("source_835").order_by("-converted_at", "-pk")[:5]
    ]
    oldest_days = max(0, (today - timezone.localtime(oldest, zone).date()).days) if oldest else 0
    return JsonResponse({
        "success": True, "client": client.name,
        "date": period["from"].isoformat() if period["from"] else None,
        "period": period_payload(period),
        "metrics": {
            "received_files": inbound.count(),
            "received_claims": inbound.aggregate(n=Sum("claims_count"))["n"] or 0,
            "converted_claims": converted.aggregate(n=Sum("claim_count"))["n"] or 0,
            "sent_files": sent.count(),
            "sent_claims": sent.aggregate(n=Sum("claim_count"))["n"] or 0,
            "waiting_claims": held_total, "oldest_days": oldest_days,
            "decisions": held_file_total + error_file_total,
            **money,
        },
        "claims": claims, "claims_limited": int(held_total) > len(claims),
        "issues": issues,
        "issues_limited": held_file_total + error_file_total > len(issues),
        "ignored_files": [key for key, value in latest_hidden.items() if value],
        "history": preview_history,
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
        page_size = min(50, max(1, int(request.GET.get("page_size", "10"))))
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid page"}, status=400)
    try:
        period = resolve_period(request.GET, client, default="all")
    except InvalidPeriod as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)
    pages = Paginator(
        apply_period(MIRFile.objects.filter(client=client), "converted_at", period)
        .select_related("source_835").order_by("-converted_at", "-pk"),
        page_size,
    )
    page = pages.get_page(number)
    return JsonResponse({
        "success": True, "page": page.number, "pages": pages.num_pages, "page_size": page_size,
        "period": period_payload(period),
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
