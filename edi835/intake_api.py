"""Client-scoped asynchronous intake. Partner authentication never uses cookies."""
import hashlib
import hmac
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods

from accounts.models import Client
from admin_panel.access_control import can_access_client
from .intake_service import register_sources
from .models import InboundTransfer, IntakePartner
from .intake_receipts import receipt_detail, receipt_summary


def _response(payload, status=200):
    response = JsonResponse(payload, status=status)
    response["Cache-Control"] = "private, no-store"
    return response


def _partner(request):
    if not settings.INTAKE_PARTNER_ENABLED or not request.is_secure():
        return None
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer ") or len(authorization) > 200:
        return None
    try:
        identifier, secret = authorization[7:].split(".", 1)
        identifier = uuid.UUID(identifier)
    except ValueError:
        return None
    partner = IntakePartner.objects.select_related("client").filter(
        pk=identifier, active=True, expires_at__gt=timezone.now(),
    ).first()
    if not partner or not hmac.compare_digest(partner.token_digest, hashlib.sha256(secret.encode()).hexdigest()):
        return None
    if partner.client.status != "ACTIVE" or partner.client.stage == "offboarded":
        return None
    return partner


def _handle(request, partner=None):
    user = getattr(request, "user", None)
    if not partner and not (user and user.is_authenticated and user.is_active and user.is_staff):
        return _response({"error": "Staff access required."}, 403)
    if request.method == "GET":
        if not request.GET.get("receipt_id"):
            from django.core.paginator import Paginator
            try:
                client = partner.client if partner else Client.objects.get(pk=request.GET.get("client_id"))
                if not partner and not can_access_client(user, client.pk):
                    return _response({"error": "Access denied."}, 403)
                page_number = int(request.GET.get("page", "1"))
                if page_number < 1:
                    raise ValueError
            except (Client.DoesNotExist, ValueError, TypeError, ValidationError):
                return _response({"error": "Select an authorized client and a valid page."}, 400)
            rows = InboundTransfer.objects.filter(client=client)
            if partner:
                rows = rows.filter(partner=partner)
            if request.GET.get("state"):
                rows = rows.filter(state=request.GET["state"])
            page = Paginator(rows.order_by("-received_at", "-pk").values(
                "id", "workflow", "channel", "state", "byte_count", "received_at", "canonical_transfer_id"), 50).get_page(page_number)
            return _response({"receipts": list(page.object_list), "page": page.number,
                              "pages": page.paginator.num_pages, "total": page.paginator.count})
        try:
            identifier = uuid.UUID(request.GET.get("receipt_id", ""))
        except ValueError:
            return _response({"error": "A valid receipt_id is required."}, 400)
        receipt = InboundTransfer.objects.select_related("dispatch").filter(pk=identifier).first()
        allowed = receipt and (receipt.partner_id == partner.pk if partner else can_access_client(user, receipt.client_id))
        if not allowed:
            return _response({"error": "Receipt not found."}, 404)
        result = receipt_detail(receipt)
        if partner:
            result["status_url"] = f"/edi835/api/intake/partner/?receipt_id={receipt.pk}"
        return _response(result)
    workflow = request.POST.get("workflow")
    upload = request.FILES.get("file")
    key = request.headers.get("Idempotency-Key", "").strip()
    if workflow not in {"835", "837"} or len(request.FILES) != 1 or len(request.FILES.getlist("file")) != 1 or not upload or not 1 <= len(key) <= 200:
        return _response({"error": "Supply one file, workflow 835/837 and an Idempotency-Key header."}, 400)
    if partner and workflow not in partner.workflows:
        return _response({"error": "Workflow is not permitted for this credential."}, 403)
    try:
        client = partner.client if partner else Client.objects.get(pk=request.POST.get("client_id"))
    except (Client.DoesNotExist, ValueError, TypeError, ValidationError):
        return _response({"error": "Select an authorized client."}, 400)
    if not partner and not can_access_client(user, client.pk):
        return _response({"error": "Access denied."}, 403)
    if upload.size > settings.INTAKE_MAX_BYTES:
        return _response({"error": "File exceeds the configured intake size limit."}, 413)
    try:
        receipts, reused = register_sources(client=client, actor=None if partner else user,
            channel="PARTNER" if partner else "STAFF", workflow=workflow,
            sources=[(upload.name, upload)], partner=partner, idempotency_key=key, asynchronous=True)
    except ValueError as exc:
        return _response({"error": str(exc)}, 409)
    except (DatabaseError, OSError):
        return _response({"error": "Intake could not be committed. Retry with the same idempotency key."}, 503)
    receipt = receipts[0]
    summary = receipt_summary(receipt)
    if partner:
        summary["status_url"] = f"/edi835/api/intake/partner/?receipt_id={receipt.pk}"
    return _response({**summary, "job_id": str(receipt.dispatch_id),
        "state": receipt.dispatch.state, "reused": reused,
        "duplicate": bool(receipt.canonical_transfer_id),
        "message": ("Duplicate received; original processing is unchanged." if receipt.canonical_transfer_id else
                    "Existing receipt returned." if reused else "Received for processing; validation and conversion are pending.")},
        200 if receipt.canonical_transfer_id or reused and receipt.dispatch.state in {"COMPLETED", "FAILED"} else 202)


@transaction.non_atomic_requests
@require_http_methods(["GET", "POST"])
@csrf_protect
def staff_intake(request):
    return _handle(request)


@transaction.non_atomic_requests
@csrf_exempt
@require_http_methods(["GET", "POST"])
def partner_intake(request):
    partner = _partner(request)
    if not partner:
        return _response({"error": "Valid partner credentials over HTTPS are required."}, 401)
    return _handle(request, partner)
