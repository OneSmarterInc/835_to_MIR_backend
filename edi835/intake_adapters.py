"""Compatibility adapters: share intake without changing converter contracts."""
from functools import wraps
from inspect import signature

from accounts.request_context import get_current_request
from .intake_service import current_transfer, finish_inline, register_sources, text_source
from .intake_storage import StoredSource, read_verified
from .intake_receipts import DuplicateSubmission, duplicate_result, receipt_summary


def _execute(receipts, function, *args, **kwargs):
    from .intake_lifecycle import begin_effects, execution, record_validation
    with execution(receipts[0].dispatch.payload):
        for receipt in receipts:
            record_validation(receipt, read_verified(receipt))
        begin_effects()
        return function(*args, **kwargs)


def track_upload_request(kind):
    """Persist intake before legacy synchronous parsing; preserve response shape."""
    def decorate(function):
        @wraps(function)
        def wrapped(request, *args, **kwargs):
            import json
            from django.db import DatabaseError
            from django.http import JsonResponse
            if request.method != "POST":
                return function(request, *args, **kwargs)
            if kind == "CLIENT_CONVERT":
                from .client_portal_views import only_client_user
                _, denied = only_client_user(request)
                if denied:
                    return denied
                if request.POST.get("confirm_send") != "yes":
                    return function(request, *args, **kwargs)
            if kind == "ANALYSIS":
                from .client_portal_analysis import MAX_BYTES, only_client_user
                client, denied = only_client_user(request, allowed_roles=("CLIENT_USER", "DEMO"))
                if denied:
                    return denied
                workflow = request.POST.get("kind")
                uploads = request.FILES.getlist("file")
                if workflow not in {"835", "837"} or len(uploads) != 1 or uploads[0].size > MAX_BYTES:
                    return function(request, *args, **kwargs)
                sources = [(item.name, item) for item in uploads]
                channel, workflow, limit = "ANALYSIS", f"ANALYSIS_{workflow}", MAX_BYTES
            else:
                from converter.views import _request_client
                try:
                    body = json.loads(request.body) if request.content_type == "application/json" else request.POST
                    client = _request_client(request, body.get("client_id") or body.get("client"))
                    if (body.get("client_id") or body.get("client")) and client is None:
                        return JsonResponse({"success": False, "error": "Select an authorized client."}, status=403)
                    uploads = (request.FILES.getlist("recon_file") if kind == "RECON" else
                               request.FILES.getlist("edi_files") or request.FILES.getlist("edi_file"))
                    sources = [(item.name, item) for item in uploads]
                    if not uploads and isinstance(body.get("files"), list):
                        sources = [(item.get("filename", "upload.835"), text_source(item.get("content", ""))) for item in body["files"]]
                    elif not uploads and body.get("edi_text"):
                        sources = [(body.get("original_filename", "pasted_file.x12"), text_source(body["edi_text"]))]
                except (ValueError, TypeError, AttributeError):
                    return function(request, *args, **kwargs)
                channel = "STAFF"
                workflow = "835" if kind in {"CONVERT_835", "CLIENT_CONVERT"} else "VALIDATE_835"
                limit = 10 * 1024 * 1024 if kind == "CLIENT_CONVERT" else None
                if kind == "RECON":
                    from django.conf import settings
                    workflow = "RECON"
                    limit = getattr(settings, "RECON_MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
            # Preserve mixed-batch behavior: empty inputs are rejected by the
            # existing validator without stopping valid siblings from processing.
            sources = [(name, stream) for name, stream in sources
                       if getattr(stream, "size", None) != 0 and
                       (not hasattr(stream, "getbuffer") or len(stream.getbuffer()) > 0)]
            if not sources:
                return function(request, *args, **kwargs)
            try:
                receipts, _ = register_sources(client=client, actor=request.user, channel=channel,
                    workflow=workflow, sources=sources, max_bytes=limit)
            except ValueError as exc:
                return JsonResponse({"success": False, "error": str(exc)}, status=400)
            except (DatabaseError, OSError):
                return JsonResponse({"success": False, "error": "Unable to durably save the upload. Please retry."}, status=503)
            for upload in uploads:
                upload.seek(0)
            all_receipts = receipts
            receipts = [item for item in receipts if not item.canonical_transfer_id]
            if not receipts:
                return JsonResponse(duplicate_result(all_receipts), status=409)
            if len(receipts) != len(all_receipts):
                # Remove duplicate inputs before the legacy combined converter is
                # called. Retain empty inputs so its existing refusal report works.
                if uploads:
                    decisions = iter(all_receipts)
                    retained = [item for item in uploads if not item.size or not next(decisions).canonical_transfer_id]
                    for key in ("edi_files", "edi_file", "file", "recon_file"):
                        if key in request.FILES:
                            request.FILES.setlist(key, retained)
                elif isinstance(body.get("files"), list):
                    decisions = iter(all_receipts)
                    body["files"] = [item for item in body["files"]
                        if not item.get("content") or not next(decisions).canonical_transfer_id]
                    request._body = json.dumps(body).encode("utf-8")
            token = current_transfer.set(receipts[0]) if workflow == "835" else None
            try:
                response = _execute(receipts, function, request, *args, **kwargs)
                body = json.loads(response.content)
                success = response.status_code < 400 and body.get("success", True) and body.get("is_valid", body.get("report", {}).get("is_valid", True))
                duplicate = body.get("duplicate") or body.get("reused") or body.get("code") == "DUPLICATE_FILE"
                file_status = body.get("file", {}).get("status")
                if file_status in {"PARTIAL", "REJECTED"}:
                    from .models import InboundTransfer
                    InboundTransfer.objects.filter(pk__in=[r.pk for r in receipts]).update(validation_outcome=file_status)
                finish_inline(receipts, success, source_id=body.get("file_id") or body.get("file", {}).get("id"),
                              outcome="DUPLICATE" if duplicate else ("REJECTED" if file_status == "REJECTED" or not success and response.status_code < 500 else None))
                body["receipts"] = [receipt_summary(item) for item in all_receipts]
                response.content = json.dumps(body)
                response["Cache-Control"] = "private, no-store"
                return response
            except Exception:
                finish_inline(receipts, False)
                raise
            finally:
                if token is not None:
                    current_transfer.reset(token)
        return wrapped
    return decorate


def tracked_processing(workflow: str, *, multiple: bool = False):
    def decorate(function):
        sig = signature(function)

        @wraps(function)
        def wrapped(*args, **kwargs):
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            client = values.get("client")
            # Worker-owned receipts are completed by the worker itself.
            active = current_transfer.get()
            if active:
                if str(active.client_id or "") != str(getattr(client, "pk", "") or "") or active.workflow != workflow:
                    raise ValueError("Intake workflow/client mismatch.")
                return function(*args, **kwargs)
            request = get_current_request()
            actor = values.get("actor") or values.get("ingestion_actor") or getattr(request, "user", None)
            mode = values.get("ingestion_source", values.get("import_mode", "SFTP" if workflow == "RECON" else "MANUAL"))
            channel = "SFTP" if str(mode).upper() in {"SFTP", "AUTO", "AUTOMATED"} else "STAFF"
            if workflow in {"837", "RECON"}:
                raw = values["raw"]
                sources = [(values["filename"], raw if isinstance(raw, StoredSource) or hasattr(raw, "read") else text_source(raw))]
            elif multiple:
                items = values["files_list"]
                if not items:
                    return function(*args, **kwargs)
                sources = [(item.get("filename") or item.get("original_filename") or f"file_{index + 1}.835",
                            item.get("_stored_source") or text_source(item.get("content") or item.get("edi_text") or ""))
                           for index, item in enumerate(items)
                           if item.get("_stored_source") or item.get("content") or item.get("edi_text")]
                if not sources:
                    return function(*args, **kwargs)
            else:
                # Old validated files may carry their client only in the DB row.
                if values.get("file_id"):
                    from .models import EDI835File
                    record = EDI835File.objects.select_related("client").filter(pk=values["file_id"]).first()
                    if record:
                        client = record.client
                sources = [(values["original_filename"], text_source(values["edi_text"]))]
            receipts, _ = register_sources(client=client, actor=actor, channel=channel,
                                           workflow=workflow, sources=sources)
            all_receipts = receipts
            receipts = [item for item in receipts if not item.canonical_transfer_id]
            if not receipts:
                raise DuplicateSubmission(all_receipts)
            if multiple and len(receipts) != len(all_receipts):
                decisions = iter(all_receipts)
                values["files_list"] = [item for item in items
                    if not (item.get("_stored_source") or item.get("content") or item.get("edi_text"))
                    or not next(decisions).canonical_transfer_id]
            if workflow in {"837", "RECON"}:
                values["raw"] = read_verified(receipts[0])
            token = current_transfer.set(receipts[0])
            try:
                result = _execute(receipts, function, *bound.args, **bound.kwargs)
                if workflow == "RECON":
                    source = result.get("file", {})
                    finish_inline(receipts, source.get("status") in {"PROCESSED", "COMPLETED", "PARTIAL"}, source_id=source.get("id"))
                    return result
                record = result[0] if workflow == "837" else result.get("db_record")
                success = True if workflow == "837" else bool(result.get("success"))
                finish_inline(receipts, success, source_id=getattr(record, "pk", None),
                              outcome="DUPLICATE" if workflow == "837" and result[1] else None)
                if workflow == "835":
                    result["receipts"] = [receipt_summary(item) for item in all_receipts]
                return result
            except Exception:
                finish_inline(receipts, False)
                raise
            finally:
                current_transfer.reset(token)
        return wrapped
    return decorate
