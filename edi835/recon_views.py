import hashlib
import os
import uuid

from django.conf import settings
from django.db import IntegrityError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from accounts.models import Client
from project835.decorators import authenticated_api_required

from .models import RECONFile
from .recon_service import process_recon_file


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
        "client_id": str(item.client_id),
        "client_name": item.client.name,
        "client_code": item.client.client_code,
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
    else:
        queryset = queryset.none()
    return JsonResponse({"success": True, "files": [_serialize_file(item) for item in queryset[:500]]})


@csrf_exempt
@authenticated_api_required
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
    if not client:
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
            stored_filename=f"{client.client_code}_{uuid.uuid4()}_{original}"[:255],
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
def recon_process(request, file_id):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Only POST is allowed."}, status=405)
    recon = _visible_file(request, file_id)
    if not recon:
        return JsonResponse({"success": False, "error": "RECON file was not found."}, status=404)
    try:
        run = process_recon_file(recon, request.user)
    except Exception as exc:
        return JsonResponse({"success": False, "error": str(exc), "file": _serialize_file(recon)}, status=400)
    recon.refresh_from_db()
    return JsonResponse({
        "success": True,
        "file": _serialize_file(recon),
        "run": {
            "id": str(run.id),
            "status": run.status,
            "claims_created": run.claims_created,
            "services_created": run.services_created,
            "invalid_records": run.invalid_records,
        },
    })


@csrf_exempt
@authenticated_api_required
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
