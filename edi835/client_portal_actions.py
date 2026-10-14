"""Real, audited client review operations.

Requests are internal workflow items, not claims-system transmissions or
automatic changes to 835/MIR business rules.
"""
import json
import uuid

from django.http import JsonResponse
from django.core.paginator import Paginator
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from project835.decorators import authenticated_api_required
from .models import ClientPortalAction, EDI835Claim, EDI835File
from .client_portal_views import only_client_user


KIND_LABELS = {
    "NOTE": "Note added",
    "REQUEST_RESEND": "Resend requested",
    "REQUEST_PLAN_ID": "Plan ID correction requested",
    "REQUEST_DICTIONARY": "Code dictionary review requested",
    "REQUEST_RELEASE": "Exception release review requested",
    "IGNORE_FILE": "File issue hidden",
    "REOPEN_FILE": "File issue reopened",
}


def actor_client(request):
    user = request.user
    if not user.is_authenticated or user.is_staff or not user.client_id:
        return None, JsonResponse({"success": False, "error": "Client account required"}, status=403)
    return user.client, None


def serialize(action):
    return {
        "id": action.pk, "kind": action.kind,
        "label": KIND_LABELS.get(action.kind, action.kind),
        "status": action.status, "message": action.message,
        "file_id": str(action.file_id),
        "file_name": action.file.original_filename,
        "claim_id": action.claim_id,
        # Internal linkage, distinct from a payer claim identifier. The demo
        # masking layer transforms fields named claim_id, but not record_id.
        "record_id": action.claim_id,
        "by": action.created_by_label or (action.created_by.name if action.created_by else "Former user"),
        "at": action.created_at.isoformat(),
        "reviewed_at": action.reviewed_at.isoformat() if action.reviewed_at else None,
    }


@csrf_exempt
@authenticated_api_required
def actions(request):
    client, denied = actor_client(request)
    if denied:
        return denied
    if request.method == "GET":
        qs = ClientPortalAction.objects.select_related("file", "created_by").filter(
            client=client
        ).only(
            "id", "kind", "status", "message", "file_id", "claim_id",
            "created_by_id", "created_by_label", "created_at", "reviewed_at",
            "file__original_filename", "created_by__name",
        ).order_by("-created_at", "-pk")
        if "claim_id" in request.GET:
            try:
                claim_id = int(request.GET["claim_id"])
                page_number = max(1, int(request.GET.get("page", "1")))
                page_size = min(50, max(1, int(request.GET.get("page_size", "10"))))
            except (TypeError, ValueError):
                return JsonResponse({"success": False, "error": "Invalid claim or pagination"}, status=400)
            if not EDI835Claim.objects.filter(pk=claim_id, edi_file__client=client).exists():
                return JsonResponse({"success": False, "error": "Claim not found"}, status=404)
            page = Paginator(qs.filter(claim_id=claim_id), page_size).get_page(page_number)
            return JsonResponse({
                "success": True, "actions": [serialize(row) for row in page],
                "page": page.number, "pages": page.paginator.num_pages,
                "count": page.paginator.count,
            })
        qs = qs[:200]
        return JsonResponse({"success": True, "actions": [serialize(row) for row in qs]})
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "GET or POST only"}, status=405)
    if request.user.client_role != "CLIENT_USER":
        return JsonResponse({"success": False, "error": "Only Client Users submit review requests"}, status=403)
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
    kind = str(data.get("kind") or "").upper()
    if kind not in KIND_LABELS:
        return JsonResponse({"success": False, "error": "Unknown review action"}, status=400)
    message = str(data.get("message") or "").strip()
    if len(message) > 2000:
        return JsonResponse({"success": False, "error": "Note cannot exceed 2000 characters"}, status=400)
    if kind == "NOTE" and not message:
        return JsonResponse({"success": False, "error": "Enter a note"}, status=400)
    try:
        file_id = uuid.UUID(str(data.get("file_id")))
    except (ValueError, TypeError, AttributeError):
        return JsonResponse({"success": False, "error": "Valid source file ID required"}, status=400)
    file = EDI835File.objects.filter(id=file_id, client=client).first()
    if file is None:
        return JsonResponse({"success": False, "error": "Source file not found"}, status=404)
    claim = None
    if data.get("claim_id") is not None:
        try:
            claim_id = int(data["claim_id"])
        except (TypeError, ValueError):
            return JsonResponse({"success": False, "error": "Invalid claim ID"}, status=400)
        claim = EDI835Claim.objects.filter(pk=claim_id, edi_file=file).first()
        if claim is None:
            return JsonResponse({"success": False, "error": "Claim not in source file"}, status=404)
    if kind.startswith("REQUEST_"):
        pending = ClientPortalAction.objects.select_related("file", "created_by").filter(
            client=client, file=file, claim=claim, kind=kind, status="OPEN"
        ).order_by("-created_at").first()
        if pending:
            return JsonResponse({
                "success": True, "action": serialize(pending),
                "message": "This request is already queued for Client Admin review.",
            })
    record = ClientPortalAction.objects.create(
        client=client, file=file, claim=claim, created_by=request.user,
        kind=kind, message=message, created_by_label=request.user.name or request.user.email,
    )
    if kind.startswith("REQUEST_"):
        # A request is a durable internal workflow record. Notify only the
        # same-tenant Client Admin accounts without emailing claim-level PHI.
        try:
            from accounts.models import User
            from admin_panel.email_service import send_client_email
            admins = list(User.objects.filter(
                client=client, client_role="CLIENT_ADMIN", is_staff=False,
                is_active=True,
            ).values_list("email", flat=True))
            if admins:
                send_client_email(
                    client,
                    "FileX: Client review request #" + str(record.pk),
                    "<p>A new client review request is waiting in FileX.</p>"
                    "<p>Request #" + str(record.pk) + " — " + KIND_LABELS[kind] +
                    ". Open <strong>Client review requests</strong> in the portal to act.</p>",
                    to_emails=admins,
                )
        except Exception:
            # SMTP problems must never erase an authenticated user's decision.
            import logging
            logging.getLogger(__name__).exception("Unable to notify Client Admin of review request")
    return JsonResponse({
        "success": True, "action": serialize(record),
        "message": "Request recorded for Client Admin review." if kind.startswith("REQUEST_")
                   else "Decision saved." if kind != "NOTE" else "Note saved.",
    }, status=201)


@csrf_exempt
@authenticated_api_required
def review_action(request, action_id):
    client, denied = actor_client(request)
    if denied:
        return denied
    if request.user.client_role != "CLIENT_ADMIN":
        return JsonResponse({"success": False, "error": "Client Admin required"}, status=403)
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "POST only"}, status=405)
    action = ClientPortalAction.objects.select_related("file", "created_by").filter(
        pk=action_id, client=client
    ).first()
    if not action:
        return JsonResponse({"success": False, "error": "Request not found"}, status=404)
    if action.status == "OPEN":
        action.status = "REVIEWED"
        action.reviewed_by = request.user
        action.reviewed_at = timezone.now()
        action.save(update_fields=["status", "reviewed_by", "reviewed_at"])
    return JsonResponse({"success": True, "action": serialize(action),
                         "message": "Marked reviewed; no claim or SFTP data was changed."})
