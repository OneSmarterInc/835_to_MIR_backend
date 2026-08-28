import hashlib
import os
import subprocess
import sys
import uuid

from django.conf import settings
from django.db import IntegrityError
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from accounts.models import Client
from project835.decorators import authenticated_api_required, json_api_errors

from .models import MIRClaim, RECONClaim, RECONFile
from .recon_service import process_recon_file
from .reconciliation_service import reconciliation_rows


def _request_client(request, supplied_client_id=None):
    user = request.user
    if getattr(user, "client_id", None):
        return user.client
    if not user.is_staff:
        return None
    if not supplied_client_id:
        return None
    try:
        return Client.objects.get(id=supplied_client_id)
    except (Client.DoesNotExist, ValueError):
        return None


def _visible_file(request, file_id):
    queryset = RECONFile.objects.select_related("client", "uploaded_by")
    if getattr(request.user, "client_id", None):
        queryset = queryset.filter(client_id=request.user.client_id)
    elif not request.user.is_staff:
        return None
    try:
        return queryset.get(id=file_id)
    except (RECONFile.DoesNotExist, ValueError):
        return None


def _serialize_file(item):
    return {
        "id": str(item.id),
        "client_id": str(item.client_id) if item.client_id else None,
        "client_name": item.client.name if item.client else "Global System Default",
        "client_code": item.client.client_code if item.client else "",
        "original_filename": item.original_filename,
        "stored_filename": item.stored_filename,
        "file_size": item.file_size,
        "record_count": item.record_count,
        "claim_count": item.claim_count,
        "service_count": item.service_count,
        "total_charge_amount": str(item.total_charge_amount),
        "total_paid_amount": str(item.total_paid_amount),
        "status": item.status,
        "processing_error": item.processing_error,
        "uploaded_by": item.uploaded_by.email if item.uploaded_by else "",
        "uploaded_at": item.uploaded_at.isoformat() if item.uploaded_at else None,
        "processing_started_at": item.processing_started_at.isoformat() if item.processing_started_at else None,
        "processed_at": item.processed_at.isoformat() if item.processed_at else None,
    }


@csrf_exempt
@authenticated_api_required
@json_api_errors
def recon_files(request):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)
    queryset = RECONFile.objects.select_related("client", "uploaded_by")
    actor_client_id = getattr(request.user, "client_id", None)
    if actor_client_id:
        queryset = queryset.filter(client_id=actor_client_id)
    elif request.user.is_staff:
        client_id = request.GET.get("client_id", "").strip()
        if client_id:
            queryset = queryset.filter(client_id=client_id)
        elif request.GET.get("scope") == "global":
            queryset = queryset.filter(client__isnull=True)
    else:
        queryset = queryset.none()
    return JsonResponse({"success": True, "files": [_serialize_file(item) for item in queryset[:500]]})


@csrf_exempt
@authenticated_api_required
@json_api_errors
def recon_upload(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST is allowed."}, status=405)
    upload = request.FILES.get("recon_file")
    if not upload:
        return JsonResponse({"success": False, "error": "Select a RECON file."}, status=400)
    max_bytes = getattr(settings, "RECON_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
    if upload.size > max_bytes:
        return JsonResponse({"success": False, "error": "RECON file exceeds the 50 MB limit."}, status=400)
    client = _request_client(request, request.POST.get("client_id"))
    if not client and not request.user.is_staff:
        message = "Select a client." if request.user.is_staff else "Your account is not associated with a client."
        return JsonResponse({"success": False, "error": message}, status=400)
    raw = upload.read()
    if not raw:
        return JsonResponse({"success": False, "error": "The RECON file is empty."}, status=400)
    text = raw.decode("utf-8-sig", errors="replace")
    file_hash = hashlib.sha256(raw).hexdigest()
    original = os.path.basename(upload.name)[:255]
    try:
        recon = RECONFile.objects.create(
            client=client,
            uploaded_by=request.user,
            original_filename=original,
            stored_filename=f"{client.client_code if client else 'GLOBAL'}_{uuid.uuid4()}_{original}"[:255],
            file_content=text,
            file_hash=file_hash,
            file_size=len(raw),
        )
    except IntegrityError:
        existing = RECONFile.objects.filter(client=client, file_hash=file_hash).first()
        return JsonResponse({
            "success": False,
            "error": "This RECON file was already uploaded for the selected client.",
            "existing_file_id": str(existing.id) if existing else None,
        }, status=409)
    return JsonResponse({"success": True, "file": _serialize_file(recon)}, status=201)


@csrf_exempt
@authenticated_api_required
@json_api_errors
def recon_process(request, file_id):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST is allowed."}, status=405)
    recon = _visible_file(request, file_id)
    if not recon:
        return JsonResponse({"success": False, "error": "RECON file was not found."}, status=404)
    if recon.status == "PROCESSING":
        return JsonResponse({"success": True, "file": _serialize_file(recon), "background": True}, status=202)
    if getattr(settings, "RECON_PROCESS_SYNCHRONOUS", False):
        try:
            process_recon_file(recon, request.user)
        except Exception as exc:
            return JsonResponse({"success": False, "error": str(exc), "file": _serialize_file(recon)}, status=400)
        recon.refresh_from_db()
        return JsonResponse({"success": True, "file": _serialize_file(recon), "background": False})
    try:
        recon.status = "PROCESSING"
        recon.processing_started_at = timezone.now()
        recon.processing_error = ""
        recon.save(update_fields=["status", "processing_started_at", "processing_error", "updated_at"])
        subprocess.Popen(
            [sys.executable, os.path.join(settings.BASE_DIR, "manage.py"), "process_recon_file", str(recon.id)],
            cwd=settings.BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        recon.status = "FAILED"
        recon.processing_error = str(exc)
        recon.save(update_fields=["status", "processing_error", "updated_at"])
        return JsonResponse({"success": False, "error": str(exc), "file": _serialize_file(recon)}, status=400)
    return JsonResponse({"success": True, "file": _serialize_file(recon), "background": True}, status=202)


@csrf_exempt
@authenticated_api_required
@json_api_errors
def recon_detail(request, file_id):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)
    recon = _visible_file(request, file_id)
    if not recon:
        return JsonResponse({"success": False, "error": "RECON file was not found."}, status=404)
    claims = [{
        "id": claim.id,
        "claim_sequence": claim.claim_sequence,
        "claim_control_number": claim.claim_control_number,
        "member_id": claim.member_id,
        "claim_status": claim.claim_status,
        "service_count": claim.service_count,
        "charge_amount": str(claim.charge_amount),
        "allowed_amount": str(claim.allowed_amount),
        "paid_amount": str(claim.paid_amount),
    } for claim in recon.claims.all()[:500]]
    errors = [{
        "row_number": error.row_number,
        "error_code": error.error_code,
        "error_message": error.error_message,
    } for error in recon.processing_errors.all()[:200]]
    return JsonResponse({"success": True, "file": _serialize_file(recon), "claims": claims, "errors": errors})


@csrf_exempt
@authenticated_api_required
@json_api_errors
def reconciliation_results(request):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)
    is_global = request.user.is_staff and request.GET.get("scope") == "global"
    client = None if is_global else _request_client(request, request.GET.get("client_id"))
    if not client and not is_global:
        return JsonResponse({"success": False, "error": "Select a client."}, status=400)
    files = RECONFile.objects.filter(client=client, status="PROCESSED").order_by("-processed_at", "-uploaded_at")[:500]
    try:
        page = max(1, int(request.GET.get("page", "1")))
        page_size = min(250, max(25, int(request.GET.get("page_size", "100"))))
    except ValueError:
        return JsonResponse({"success": False, "error": "Invalid page parameters."}, status=400)
    sort_by = request.GET.get("sort_by", "")
    sort_direction = request.GET.get("sort_direction", "asc")
    allowed_sorts = {
        "", "claim_id", "patient_name", "mir_filename", "recon_filename",
        "amount_to_pay", "recon_paid_amount", "difference_amount", "status",
    }
    if sort_by not in allowed_sorts or sort_direction not in {"asc", "desc"}:
        return JsonResponse({"success": False, "error": "Invalid sort parameters."}, status=400)
    claims, total = reconciliation_rows(
        client, files, page=page, page_size=page_size, search=request.GET.get("search", ""),
        sort_by=sort_by, sort_direction=sort_direction,
    )
    return JsonResponse({
        "success": True,
        "recon_files": [_serialize_file(item) for item in files],
        "claims": claims,
        "total_claims": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    })


@csrf_exempt
@authenticated_api_required
@json_api_errors
def reconciliation_claim_detail(request, claim_id):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)
    queryset = MIRClaim.objects.select_related("mir_file", "mir_file__client")
    if getattr(request.user, "client_id", None):
        queryset = queryset.filter(mir_file__client_id=request.user.client_id)
    elif not request.user.is_staff:
        queryset = queryset.none()
    try:
        claim = queryset.get(id=claim_id)
    except (MIRClaim.DoesNotExist, ValueError):
        return JsonResponse({"success": False, "error": "MIR claim was not found."}, status=404)
    recon_files = RECONFile.objects.filter(client=claim.mir_file.client, status="PROCESSED")
    row = reconciliation_rows(claim.mir_file.client, recon_files, claim_id=claim.id)
    summary = next((item for item in row if item["mir_claim_id"] == claim.id), None)
    recon_claims = list(RECONClaim.objects.filter(recon_file__in=recon_files).select_related("recon_file").filter(
        claim_control_number__iexact=claim.claim_control_number
    ))
    mir_services = [{
        "sequence": item.service_sequence, "procedure_code": item.procedure_code,
        "service_date": item.service_date, "units": str(item.units),
        "charge_amount": str(item.charge_amount), "paid_amount": str(item.paid_amount),
        "patient_liability": str(item.patient_liability), "reason_code": item.reason_code,
    } for item in claim.service_lines.all()]
    recon_services = [{
        "sequence": item.service_sequence, "procedure_code": item.procedure_code,
        "revenue_code": item.revenue_code, "service_from_date": item.service_from_date,
        "service_to_date": item.service_to_date, "units": str(item.units),
        "charge_amount": str(item.charge_amount), "allowed_amount": str(item.allowed_amount),
        "paid_amount": str(item.paid_amount), "patient_responsibility": str(item.patient_responsibility),
        "adjustment_amount": str(item.adjustment_amount), "reason_code": item.reason_code,
    } for recon_claim in recon_claims for item in recon_claim.service_lines.all()]
    recon_names = list(dict.fromkeys(item.recon_file.original_filename for item in recon_claims))
    latest_recon_claim = recon_claims[-1] if recon_claims else None
    return JsonResponse({
        "success": True, "summary": summary,
        "mir": {"file": claim.mir_file.mir_filename, "date": claim.mir_file.converted_at.isoformat(),
                "claim": claim.segment_data, "services": mir_services},
        "recon": {"file": ", ".join(recon_names),
                  "date": latest_recon_claim.recon_file.processed_at.isoformat() if latest_recon_claim and latest_recon_claim.recon_file.processed_at else None,
                  "claim": latest_recon_claim.segment_data if latest_recon_claim else None, "services": recon_services},
    })
