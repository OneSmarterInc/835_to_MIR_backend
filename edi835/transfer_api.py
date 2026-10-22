"""V1 receipt API: staff sessions + CSRF, or HTTPS partner credentials."""
import json
from datetime import datetime, time, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, Paginator
from django.db import DatabaseError, transaction
from django.http import FileResponse
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET

from accounts.models import Client
from admin_panel.access_control import can_access_client, scope_client_queryset
from .intake_api import _partner, _response
from .partner_auth import machine_request, allowed_workflows, permitted
from .intake_receipts import receipt_detail
from .intake_storage import open_verified
from .models import InboundTransfer, IntakeOutput, SubmissionKey
from .intake_access import grants, permitted_staff, audit_access
from .partner_auth import client_ids, client_permitted, resolve_client, consume_rate, PartnerClientDenied
from .transfer_service import (
    SubmissionConflict, SubmissionPending, key_digest, retry_blocker, retry_transfer, submit,
)

STATES = {"RECEIVED", "VALIDATING", "QUEUED", "PROCESSING", "COMPLETED", "REJECTED", "FAILED", "DUPLICATE"}
CHANNELS = {"STAFF", "SFTP", "PARTNER", "ANALYSIS", "SYSTEM"}


@require_GET
def openapi(request):
    """Public contract only; contains no tenant data or credentials."""
    return FileResponse((settings.BASE_DIR / "docs/openapi/transfers-v1.json").open("rb"),
                        content_type="application/json")


def error(message, status=400, code="INVALID_REQUEST"):
    return _response({"error": message, "code": code}, status)


def scoped(request, partner):
    rows = InboundTransfer.objects.select_related("dispatch", "client", "partner", "canonical_transfer")
    if partner:
        return rows.filter(client_id__in=client_ids(partner), partner=partner, workflow__in=allowed_workflows(partner, "READ"))
    clients = Client.objects.filter(pk__in=grants(request.user).values("client_id")).exclude(stage="offboarded")
    return rows.filter(client__in=clients)


def summary(receipt):
    url = f"/api/v1/transfers/{receipt.pk}"
    return {"receipt_id": str(receipt.pk), "status_url": url, "client_id": str(receipt.client_id),
        "workflow": receipt.workflow, "channel": receipt.channel, "state": receipt.state,
        "lifecycle_state": receipt.state, "received_at": receipt.received_at.isoformat(),
        "byte_count": receipt.byte_count, "filename": receipt.original_filename,
        "canonical_receipt_id": str(receipt.canonical_transfer_id) if receipt.canonical_transfer_id else None}


def outputs_for(receipt):
    rows = IntakeOutput.objects.filter(dispatch=receipt.dispatch)
    if receipt.dispatch.transfers.count() != 1:
        rows = rows.filter(source_transactions__receipt=receipt).distinct()
    return rows


def detail(receipt, *, operator=False):
    value = receipt_detail(receipt)
    value.update(summary(receipt))
    value["source_url"] = f"{value['status_url']}/source"
    value["outputs"] = [{"id": str(item.pk), "checksum": item.checksum, "byte_count": item.byte_count,
        "delivery_state": item.delivery_state, "download_url": f"{value['status_url']}/outputs/{item.pk}"}
        for item in outputs_for(receipt)]
    # Do not expose arbitrary worker payloads or operator audit text to partners.
    value.pop("result", None)
    blocker = retry_blocker(receipt) if operator else "Operator session required."
    value["retry_eligible"] = operator and not blocker
    if operator:
        value["retry_blocked_reason"] = blocker
        value["retries"] = list(receipt.retries.order_by("-created_at").values(
            "id", "reason", "actor_reference", "parent_id", "attempt_id", "created_at")[:20])
    return value


def listing(request, partner):
    # History needs metadata only, never dispatch payloads or source details.
    rows = scoped(request, partner).select_related(None).only(
        "id", "client_id", "workflow", "channel", "state", "received_at",
        "byte_count", "original_filename", "canonical_transfer_id",
    )
    client_id = request.GET.get("client_id")
    if client_id:
        try:
            client = Client.objects.get(pk=client_id)
        except (Client.DoesNotExist, ValueError, ValidationError):
            return error("Client not found.", 404, "NOT_FOUND")
        if (partner and not client_permitted(partner, client.pk)) or (not partner and not permitted_staff(request.user, client.pk)):
            return error("Client not found.", 404, "NOT_FOUND")
        rows = rows.filter(client=client)
    try:
        for field, allowed in (("status", STATES), ("channel", CHANNELS)):
            value = request.GET.get(field)
            if value:
                if value not in allowed:
                    raise ValueError
                rows = rows.filter(**{"state" if field == "status" else field: value})
        dates = {}
        for field in ("date_from", "date_to"):
            if request.GET.get(field):
                dates[field] = parse_date(request.GET[field])
                if dates[field] is None:
                    raise ValueError
        if len(dates) == 2 and dates["date_from"] > dates["date_to"]:
            raise ValueError
        if "date_from" in dates:
            rows = rows.filter(received_at__gte=datetime.combine(dates["date_from"], time.min, datetime_timezone.utc))
        if "date_to" in dates:
            rows = rows.filter(received_at__lt=datetime.combine(dates["date_to"] + timedelta(days=1), time.min, datetime_timezone.utc))
        number, size = int(request.GET.get("page", "1")), int(request.GET.get("page_size", "25"))
        if number < 1 or not 1 <= size <= 100:
            raise ValueError
        page = Paginator(rows.order_by("-received_at", "-pk"), size).page(number)
    except (ValueError, OverflowError, EmptyPage):
        return error("Invalid filter or page. Dates use YYYY-MM-DD in UTC; page size is 1–100.")
    audit_access(request, "RECEIPT_LIST", count=len(page.object_list))
    return _response({"transfers": [summary(item) for item in page], "page": page.number,
        "page_size": size, "pages": page.paginator.num_pages, "total": page.paginator.count})


def upload(request, partner):
    if request.content_type != "multipart/form-data":
        return error("Use multipart/form-data with one file.", 415, "UNSUPPORTED_MEDIA_TYPE")
    workflow, file = request.POST.get("workflow"), request.FILES.get("file")
    if workflow not in {"835", "837"} or not file or len(request.FILES) != 1 or len(request.FILES.getlist("file")) != 1:
        return error("Supply one file and workflow 835 or 837.")
    if partner and not permitted(partner, "SUBMIT", workflow):
        return error("Workflow is not permitted.", 403, "FORBIDDEN")
    try:
        digest = key_digest(request.headers.get("Idempotency-Key", ""), user=request.user, partner=partner)
        client = resolve_client(partner, request.POST.get("client_id")) if partner else Client.objects.get(pk=request.POST.get("client_id"))
    except PartnerClientDenied:
        return error("Client not found.", 404, "NOT_FOUND")
    except (ValueError, TypeError, ValidationError, Client.DoesNotExist):
        return error("Supply a valid Idempotency-Key and authorized client_id.")
    if partner and request.POST.get("client_id") and request.POST["client_id"] != str(client.pk):
        return error("Client not found.", 404, "NOT_FOUND")
    if not partner and not can_access_client(request.user, client.pk):
        return error("Client not found.", 404, "NOT_FOUND")
    if client.status != "ACTIVE" or client.stage == "offboarded":
        return error("Client is not active.", 403, "FORBIDDEN")
    if file.size > settings.INTAKE_MAX_BYTES:
        return error("File exceeds the intake size limit.", 413, "UPLOAD_TOO_LARGE")
    if (partner and file.size > partner.storage_limit_bytes
            and not SubmissionKey.objects.filter(pk=digest).exists()):
        return error("File exceeds the partner storage quota.", 429, "PARTNER_STORAGE_LIMIT")
    try:
        receipt, reused = submit(client=client, user=None if partner else request.user,
            partner=partner, workflow=workflow, upload=file, digest=digest,
            partner_credential=getattr(request, "partner_credential", None))
    except SubmissionConflict as exc:
        response = error(str(exc), 409, exc.code)
        if isinstance(exc, SubmissionPending):
            response["Retry-After"] = "2"
        return response
    except ValueError as exc:
        from .intake_control import QuotaExceeded
        if isinstance(exc, QuotaExceeded):
            return error(str(exc), 429, "PARTNER_STORAGE_LIMIT")
        return error(str(exc), 400)
    except (DatabaseError, OSError):
        response = error("Receipt could not be confirmed. Recover or retry with the same key.", 503, "INTAKE_UNAVAILABLE")
        response["Retry-After"] = "2"
        return response
    response = _response({**summary(receipt), "reused": reused}, 200 if reused else 202)
    response["Location"] = f"/api/v1/transfers/{receipt.pk}"
    return response


def handle(request, partner=None, receipt_id=None, action=None, output_id=None):
    if action == "retry":
        methods = {"POST"}
    elif action or receipt_id:
        methods = {"GET"}
    else:
        methods = {"GET", "POST"}
    if request.method not in methods:
        response = error("Method not allowed.", 405, "METHOD_NOT_ALLOWED")
        response["Allow"] = ", ".join(sorted(methods))
        return response
    if action == "recover":
        try:
            digest = key_digest(request.headers.get("Idempotency-Key", ""), user=request.user, partner=partner)
        except ValueError as exc:
            return error(str(exc))
        key = SubmissionKey.objects.filter(pk=digest, receipt__in=scoped(request, partner)).first()
        if not key:
            return error("No committed receipt is visible for this key. Retry the upload with the same key.", 404, "NOT_FOUND")
        audit_access(request, "RECEIPT_RECOVER", receipt=key.receipt)
        return _response(summary(key.receipt))
    if not receipt_id:
        return upload(request, partner) if request.method == "POST" else listing(request, partner)
    receipt = scoped(request, partner).filter(pk=receipt_id).first()
    if not receipt:
        audit_access(request, "RECEIPT_ACCESS", result="DENIED")
        return error("Transfer not found.", 404, "NOT_FOUND")
    if action == "retry":
        if partner or not permitted_staff(request.user, receipt.client_id, "RETRY"):
            return error("Operator session required.", 403, "FORBIDDEN")
        try:
            if request.content_type != "application/json":
                return error("Use application/json.", 415, "UNSUPPORTED_MEDIA_TYPE")
            data = json.loads(request.body)
            reason = data.get("reason") if isinstance(data, dict) else None
            if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500 or any(ord(c) < 32 for c in reason):
                return error("Supply a reason of 3–500 characters without control characters.")
            audit = retry_transfer(receipt, request.user, reason.strip())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return error("Invalid JSON.")
        except ValueError as exc:
            return error(str(exc), 409, "RETRY_NOT_ELIGIBLE")
        except (OSError, DatabaseError):
            return error("Retry could not be confirmed. Check receipt status before trying again.", 503, "RETRY_UNAVAILABLE")
        return _response({**summary(receipt), "retry_id": str(audit.pk)}, 202)
    if action in {"source", "output"}:
        if ((partner and not permitted(partner, "DOWNLOAD", receipt.workflow))
                or (not partner and not permitted_staff(request.user, receipt.client_id, "DOWNLOAD"))):
            audit_access(request, "FILE_DOWNLOAD", receipt=receipt, result="DENIED")
            return error("Download permission required.", 403, "FORBIDDEN")
        source = receipt if action == "source" else outputs_for(receipt).filter(pk=output_id).first()
        if not source:
            return error("Output not found.", 404, "NOT_FOUND")
        try:
            handle = open_verified(source)
        except (ValueError, OSError):
            return error("File is unavailable or failed its integrity check.", 503, "FILE_UNAVAILABLE")
        try:
            audit_access(request, "FILE_DOWNLOAD", receipt=receipt, kind=action,
                         output_id=str(output_id) if output_id else None)
        except Exception:
            handle.close()
            raise
        response = FileResponse(handle, as_attachment=True,
            filename=receipt.original_filename if action == "source" else f"{source.pk}.mir",
            content_type="application/octet-stream")
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    audit_access(request, "RECEIPT_DETAIL", receipt=receipt)
    value = detail(receipt, operator=partner is None and permitted_staff(request.user, receipt.client_id, "RETRY"))
    if partner is None:
        value["recovery_reviews"] = list(receipt.recovery_reviews.order_by("-created_at").values(
            "id", "decision", "evidence_reference", "created_at")[:20])
    download = permitted(partner, "DOWNLOAD", receipt.workflow) if partner else permitted_staff(request.user, receipt.client_id, "DOWNLOAD")
    if not download:
        value.pop("source_url", None)
        for output in value["outputs"]:
            output.pop("download_url", None)
    return _response(value)


@csrf_protect
def staff_view(request, **kwargs):
    user = request.user
    if not user.is_authenticated:
        return error("Sign in to continue. Your submission key remains valid.", 401, "AUTHENTICATION_REQUIRED")
    if not user.is_active or not user.is_staff:
        return error("Staff access required.", 403, "FORBIDDEN")
    return handle(request, **kwargs)


@transaction.non_atomic_requests
@csrf_exempt
def transfers(request, **kwargs):
    # A bad bearer token must never fall back to a privileged browser session.
    if machine_request(request):
        partner = _partner(request)
        if not partner:
            return error("Valid partner credentials over HTTPS are required.", 401, "AUTHENTICATION_REQUIRED")
        if not consume_rate(partner):
            response = error("Partner request quota exceeded.", 429, "PARTNER_RATE_LIMIT")
            response["Retry-After"] = "60"
            return response
        return handle(request, partner=partner, **kwargs)
    return staff_view(request, **kwargs)
