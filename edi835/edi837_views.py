import os

from django.conf import settings
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.models import Client
from project835.decorators import authenticated_api_required, json_api_errors

from .edi837_service import export_single_claim, ingest_837
from .file_types import has_valid_file_extension
from .models import EDI837Claim, EDI837File


def _client_for_request(request, supplied_id=None):
    if getattr(request.user, "client_id", None):
        return request.user.client
    if not request.user.is_staff or not supplied_id:
        return None
    try:
        client = Client.objects.get(id=supplied_id)
    except (Client.DoesNotExist, ValueError):
        return None
    if request.user.is_superuser:
        return client
    from admin_panel.access_control import has_active_client_grant
    return client if has_active_client_grant(request.user, client.id) else None


def _visible_claim(request, claim_id):
    queryset = EDI837Claim.objects.select_related("edi_file", "client")
    if getattr(request.user, "client_id", None):
        queryset = queryset.filter(client_id=request.user.client_id)
    elif not request.user.is_staff:
        return None
    elif not request.user.is_superuser:
        visible = request.user.client_access_grants.filter(
            revoked_at__isnull=True, expires_at__gt=timezone.now()
        ).values_list("client_id", flat=True)
        queryset = queryset.filter(client_id__in=visible)
    try:
        return queryset.get(id=claim_id)
    except (EDI837Claim.DoesNotExist, ValueError):
        return None


def _claim_row(claim):
    patient = " ".join(part for part in (claim.patient_first_name, claim.patient_last_name) if part)
    return {
        "id": claim.id,
        "claim_number": claim.claim_control_number,
        "highmark_claim_number": claim.highmark_claim_number,
        "internal_claim_number": claim.internal_claim_number,
        "reference_9c": claim.reference_9c,
        "patient_control_number": claim.patient_control_number,
        "member_id": claim.member_id,
        "patient_name": patient,
        "total_charge_amount": str(claim.total_charge_amount),
        "service_count": claim.service_count,
        "service_from_date": claim.service_from_date,
        "service_to_date": claim.service_to_date,
        "file_name": claim.edi_file.original_filename,
        "processed_at": claim.edi_file.processed_at.isoformat() if claim.edi_file.processed_at else None,
        "import_mode": claim.edi_file.import_mode,
    }


@csrf_exempt
@authenticated_api_required
@json_api_errors
def edi837_upload_process(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST is allowed."}, status=405)
    client = _client_for_request(request, request.POST.get("client_id"))
    if client is None:
        return JsonResponse({"success": False, "error": "Select an authorized client."}, status=400)
    uploads = request.FILES.getlist("files") or request.FILES.getlist("file")
    if not uploads:
        return JsonResponse({"success": False, "error": "Select one or more 837 files."}, status=400)
    max_bytes = getattr(settings, "EDI837_MAX_UPLOAD_BYTES", 100 * 1024 * 1024)
    results, errors = [], []
    for upload in uploads:
        try:
            if not has_valid_file_extension(upload.name, "837"):
                raise ValueError("Unsupported file extension.")
            if upload.size > max_bytes:
                raise ValueError("File exceeds the 100 MB limit.")
            edi_file, duplicate = ingest_837(client, request.user, upload.name, upload.read(), import_mode="MANUAL")
            results.append({"id": str(edi_file.id), "name": edi_file.original_filename,
                            "status": edi_file.status, "already_exists": duplicate,
                            "claim_count": edi_file.claim_count, "service_count": edi_file.service_count,
                            "total_charge_amount": str(edi_file.total_charge_amount)})
        except Exception as exc:
            errors.append({"name": os.path.basename(upload.name), "error": str(exc)})
    return JsonResponse({
        "success": bool(results), "files": results, "errors": errors,
        "processed_count": sum(1 for item in results if not item["already_exists"]),
        "duplicate_count": sum(1 for item in results if item["already_exists"]),
        "failed_count": len(errors),
        "error": "No 837 files could be processed." if not results else "",
    }, status=200 if results else 400)


@authenticated_api_required
@json_api_errors
def edi837_search(request):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)
    client = _client_for_request(request, request.GET.get("client_id"))
    if client is None:
        return JsonResponse({"success": False, "error": "Select an authorized client."}, status=400)
    query = str(request.GET.get("q") or "").strip()
    claims = EDI837Claim.objects.filter(client=client).select_related("edi_file")
    if query:
        claims = claims.filter(
            Q(claim_control_number__icontains=query) | Q(highmark_claim_number__icontains=query)
            | Q(internal_claim_number__icontains=query) | Q(patient_control_number__icontains=query)
            | Q(reference_9c__icontains=query)
        )
    else:
        claims = claims.none()
    try:
        limit = min(200, max(1, int(request.GET.get("limit", "50"))))
    except ValueError:
        limit = 50
    rows = [_claim_row(claim) for claim in claims.order_by("claim_control_number", "-edi_file__processed_at")[:limit]]
    return JsonResponse({"success": True, "query": query, "count": len(rows), "results": rows})


@authenticated_api_required
@json_api_errors
def edi837_claim_detail(request, claim_id):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)
    claim = _visible_claim(request, claim_id)
    if claim is None:
        return JsonResponse({"success": False, "error": "837 claim was not found."}, status=404)
    services = [{
        "sequence": line.service_sequence, "procedure_code": line.procedure_code,
        "qualifier": line.procedure_qualifier, "modifiers": line.modifiers,
        "revenue_code": line.revenue_code, "service_from_date": line.service_from_date,
        "service_to_date": line.service_to_date, "units": str(line.units),
        "charge_amount": str(line.charge_amount), "diagnosis_pointers": line.diagnosis_pointers,
    } for line in claim.service_lines.all()]
    return JsonResponse({"success": True, "claim": {
        **_claim_row(claim), "client_name": claim.client.name,
        "subscriber_name": " ".join(part for part in (claim.subscriber_first_name, claim.subscriber_last_name) if part),
        "billing_provider": claim.billing_provider_name, "rendering_provider": claim.rendering_provider_name,
        "referring_provider": claim.referring_provider_name,
        "payer": claim.payer_name, "claim_type": claim.claim_type,
        "place_of_service": claim.place_of_service, "claim_frequency_code": claim.claim_frequency_code,
        "original_claim_number": claim.original_claim_number, "diagnosis_codes": claim.diagnosis_codes,
        "services": services,
    }})


@authenticated_api_required
@json_api_errors
def edi837_claim_export(request, claim_id):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)
    claim = _visible_claim(request, claim_id)
    if claim is None:
        return JsonResponse({"success": False, "error": "837 claim was not found."}, status=404)
    content = export_single_claim(claim)
    safe_claim = "".join(char for char in claim.claim_control_number if char.isalnum() or char in "-_") or str(claim.id)
    filename = f"837_{safe_claim}.837"
    response = HttpResponse(content, content_type="application/edi-x12; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-OneSmarter-Filename"] = filename
    return response
