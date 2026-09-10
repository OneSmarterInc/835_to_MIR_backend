import json
import os
import tempfile
from datetime import datetime
from email.utils import parsedate_to_datetime

import extract_msg

from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from accounts.models import Client
from admin_panel.access_control import can_access_client, scope_client_queryset
from .models import EDI835File, EDI837File, MIRFile, MPLNotice, MPLNoticeClaim, RECONFile
from .mpl_notices import NoticeValidationError, parse_subject, process_notice, serialize_notice


def _body(request):
    try:
        return json.loads(request.body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NoticeValidationError("Request body must be valid JSON.")


def _parse_msg_upload(upload):
    filename = os.path.basename(str(upload.name or "email.msg"))
    if not filename.lower().endswith(".msg"):
        raise NoticeValidationError("Upload an Outlook .msg email file.")
    if upload.size > 10 * 1024 * 1024:
        raise NoticeValidationError("The .msg file must be 10 MB or smaller.")
    raw = upload.read()
    if not raw:
        raise NoticeValidationError("The selected .msg file is empty.")

    temp_path = None
    message = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as temp:
            temp.write(raw)
            temp_path = temp.name
        message = extract_msg.Message(temp_path)
        subject = str(message.subject or "").strip()
        body = str(message.body or "").strip()
        sender = str(message.sender or "").strip()
        received_at = message.date
        if received_at and not isinstance(received_at, datetime):
            try:
                received_at = parsedate_to_datetime(str(received_at))
            except (TypeError, ValueError, OverflowError):
                received_at = None
        if received_at and timezone.is_naive(received_at):
            received_at = timezone.make_aware(received_at)
        if not subject:
            raise NoticeValidationError("The .msg file does not contain an email subject.")
        if not body:
            raise NoticeValidationError("The .msg file does not contain a readable email body.")
        return {
            "filename": filename[:255],
            "content_type": str(upload.content_type or "application/vnd.ms-outlook")[:100],
            "raw": raw,
            "subject": subject,
            "body": body,
            "sender": sender,
            "received_at": received_at,
        }
    except NoticeValidationError:
        raise
    except Exception as exc:
        raise NoticeValidationError("The selected file is not a readable Outlook .msg email.") from exc
    finally:
        if message is not None:
            try:
                message.close()
            except Exception:
                pass
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def _client_for_request(request, requested_id=None):
    client_id = getattr(request.user, "client_id", None) or requested_id
    if not client_id or not can_access_client(request.user, client_id):
        raise PermissionError("You do not have access to this client.")
    return Client.objects.get(pk=client_id)


@require_http_methods(["GET", "POST"])
def mpl_notices(request):
    if request.method == "GET":
        queryset = scope_client_queryset(MPLNotice.objects.select_related("client"), request.user)
        client_id = request.GET.get("client_id")
        if client_id:
            if not can_access_client(request.user, client_id):
                return JsonResponse({"success": False, "error": "Access denied."}, status=403)
            queryset = queryset.filter(client_id=client_id)
        notices = [serialize_notice(item) for item in queryset[:100]]
        return JsonResponse({"success": True, "notices": notices})
    try:
        upload = request.FILES.get("email_file")
        if upload:
            data = request.POST
            parsed_upload = _parse_msg_upload(upload)
            client = _client_for_request(request, data.get("client_id"))
            subject = parsed_upload["subject"]
            email_body = parsed_upload["body"]
            received_at = parsed_upload["received_at"]
            reporting_year = data.get("reporting_year") or None
            parsed = parse_subject(subject, reporting_year, received_at)
            claim_numbers = [
                part.strip() for part in str(data.get("claim_numbers") or "").split(",")
                if part.strip()
            ]
            notice = MPLNotice.objects.create(
                client=client,
                subject=subject,
                sender_text=parsed_upload["sender"][:255],
                received_at=received_at,
                reporting_year=reporting_year,
                reporting_period_start=parsed["period_start"],
                reporting_period_end=parsed["period_end"],
                program=parsed["program"],
                notice_type=parsed["notice_type"],
                raw_email_body=email_body,
                requested_claim_numbers=claim_numbers[:50],
                source_filename=parsed_upload["filename"],
                source_content_type=parsed_upload["content_type"],
                source_file=parsed_upload["raw"],
                created_by=request.user,
            )
        else:
            # Retain JSON intake compatibility for existing API clients.
            data = _body(request)
            client = _client_for_request(request, data.get("client_id"))
            subject = str(data.get("subject") or "").strip()
            email_body = str(data.get("email_body") or "").strip()
            if not email_body:
                raise NoticeValidationError("Email content is required.")
            if len(email_body) > 200_000:
                raise NoticeValidationError("Email content is too large.")
            received_at = None
            if data.get("received_at"):
                received_at = datetime.fromisoformat(str(data["received_at"]).replace("Z", "+00:00"))
                if timezone.is_naive(received_at):
                    received_at = timezone.make_aware(received_at)
            parsed = parse_subject(subject, data.get("reporting_year"), received_at)
            claim_numbers = data.get("claim_numbers") or []
            if isinstance(claim_numbers, str):
                claim_numbers = [part.strip() for part in claim_numbers.split(",") if part.strip()]
            notice = MPLNotice.objects.create(
                client=client, subject=subject, sender_text=str(data.get("sender") or "")[:255],
                received_at=received_at, reporting_year=data.get("reporting_year"),
                reporting_period_start=parsed["period_start"], reporting_period_end=parsed["period_end"],
                program=parsed["program"], notice_type=parsed["notice_type"],
                raw_email_body=email_body, requested_claim_numbers=claim_numbers[:50],
                created_by=request.user,
            )
        return JsonResponse({"success": True, "notice": serialize_notice(notice, detail=True)}, status=201)
    except PermissionError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=403)
    except (NoticeValidationError, ValueError, Client.DoesNotExist) as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)


@require_http_methods(["GET"])
def mpl_notice_detail(request, notice_id):
    notice = MPLNotice.objects.select_related("client").filter(pk=notice_id).first()
    if not notice:
        return JsonResponse({"success": False, "error": "Notice not found."}, status=404)
    if not can_access_client(request.user, notice.client_id):
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)
    return JsonResponse({"success": True, "notice": serialize_notice(notice, detail=True)})


@require_http_methods(["POST"])
def mpl_notice_analyze(request, notice_id):
    notice = MPLNotice.objects.filter(pk=notice_id).first()
    if not notice:
        return JsonResponse({"success": False, "error": "Notice not found."}, status=404)
    if not can_access_client(request.user, notice.client_id):
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)
    if notice.status in {"PARSING_EMAIL", "MATCHING_CLAIMS", "COLLECTING_EVIDENCE", "RUNNING_VALIDATIONS", "ANALYZING"}:
        return JsonResponse({"success": False, "error": "This notice is already being processed."}, status=409)
    notice.status, notice.last_error = "RECEIVED", ""
    notice.save(update_fields=["status", "last_error", "updated_at"])
    return JsonResponse({"success": True, "notice": serialize_notice(notice, detail=True)})


@require_http_methods(["POST"])
def mpl_notice_select_claim(request, notice_id):
    notice = MPLNotice.objects.filter(pk=notice_id).first()
    if not notice:
        return JsonResponse({"success": False, "error": "Notice not found."}, status=404)
    if not can_access_client(request.user, notice.client_id):
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)
    try:
        link = MPLNoticeClaim.objects.select_related("claim").get(notice=notice, claim_id=int(_body(request).get("claim_id")))
    except (MPLNoticeClaim.DoesNotExist, TypeError, ValueError, NoticeValidationError):
        return JsonResponse({"success": False, "error": "Select one of the matched claims."}, status=400)
    notice.notice_claims.exclude(pk=link.pk).delete()
    link.confirmed_by_user, link.confirmed_at, link.matching_confidence = True, timezone.now(), 1
    link.save()
    # Requeue; process_notice preserves the exact requested identifier and now has one candidate.
    notice.requested_claim_numbers = [link.claim.claim_control_number]
    notice.status, notice.last_error = "RECEIVED", ""
    notice.save()
    return JsonResponse({"success": True, "notice": serialize_notice(notice, detail=True)})


@require_http_methods(["POST"])
def mpl_notice_process_now(request, notice_id):
    """Operational/test endpoint; production UI normally uses the background worker."""
    notice = MPLNotice.objects.filter(pk=notice_id).first()
    if not notice:
        return JsonResponse({"success": False, "error": "Notice not found."}, status=404)
    if not can_access_client(request.user, notice.client_id):
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)
    process_notice(notice.id)
    notice.refresh_from_db()
    return JsonResponse({"success": True, "notice": serialize_notice(notice, detail=True)})


@require_http_methods(["POST"])
def mpl_analysis_review(request, notice_id, claim_id):
    notice = MPLNotice.objects.filter(pk=notice_id).first()
    if not notice:
        return JsonResponse({"success": False, "error": "Notice not found."}, status=404)
    if not can_access_client(request.user, notice.client_id):
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)
    try:
        status = str(_body(request).get("review_status") or "").upper()
    except NoticeValidationError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=400)
    if status not in {"APPROVED", "CHANGES_REQUIRED"}:
        return JsonResponse({"success": False, "error": "Invalid review status."}, status=400)
    link = MPLNoticeClaim.objects.filter(notice=notice, claim_id=claim_id).select_related("analysis").first()
    if not link or not hasattr(link, "analysis"):
        return JsonResponse({"success": False, "error": "Analysis not found."}, status=404)
    analysis = link.analysis
    analysis.review_status, analysis.reviewed_by, analysis.reviewed_at = status, request.user, timezone.now()
    analysis.save(update_fields=["review_status", "reviewed_by", "reviewed_at", "updated_at"])
    return JsonResponse({"success": True, "notice": serialize_notice(notice, detail=True)})


@require_http_methods(["GET"])
def mpl_related_file(request, file_type, file_id):
    models = {
        "837": (EDI837File, "file_content", "original_filename"),
        "835": (EDI835File, "input_file_content", "original_filename"),
        "mir": (MIRFile, "file_content", "mir_filename"),
        "recon": (RECONFile, "file_content", "original_filename"),
    }
    config = models.get(file_type.lower())
    if not config:
        return JsonResponse({"success": False, "error": "Unsupported file type."}, status=404)
    model, content_field, filename_field = config
    record = model.objects.filter(pk=file_id).first()
    if not record:
        return JsonResponse({"success": False, "error": "File not found."}, status=404)
    if not can_access_client(request.user, record.client_id):
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)
    content = getattr(record, content_field, "") or ""
    filename = getattr(record, filename_field, "mpl-evidence.txt")
    response = HttpResponse(content.encode("utf-8"), content_type="application/octet-stream")
    response["Content-Disposition"] = f'attachment; filename="{filename.replace(chr(34), "")}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_http_methods(["GET"])
def mpl_notice_source_file(request, notice_id):
    notice = MPLNotice.objects.filter(pk=notice_id).first()
    if not notice:
        return JsonResponse({"success": False, "error": "Notice not found."}, status=404)
    if not can_access_client(request.user, notice.client_id):
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)
    if not notice.source_file:
        return JsonResponse({"success": False, "error": "Original email file is not available."}, status=404)
    filename = os.path.basename(notice.source_filename or "email.msg").replace('"', "")
    response = HttpResponse(
        bytes(notice.source_file),
        content_type=notice.source_content_type or "application/vnd.ms-outlook",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response
