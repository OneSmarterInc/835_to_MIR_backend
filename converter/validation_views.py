import json
import logging

from django.http import JsonResponse

from edi835.models import EDI835File

from .views import (
    _invalid_835_batch_response,
    _invalid_835_response,
    _offboarded_client_response,
    _request_client,
    _send_validation_notice,
    _validate_835_for_conversion,
)

logger = logging.getLogger("converter")


# 2026-09-23 - Yash: Removed csrf_exempt decorator for CSRF protection
def api_validate(request):
    """Validate one or more EDI 835 files with the existing local validation engine."""
    if request.method != "POST":
        return JsonResponse({"error": "Only POST method is allowed."}, status=405)

    body_client_id = None
    if request.content_type == "application/json":
        try:
            body = json.loads(request.body.decode("utf-8"))
            body_client_id = body.get("client_id") or body.get("client")
        except Exception:
            body = {}
    else:
        body = {}
        body_client_id = request.POST.get("client_id") or request.POST.get("client")

    client = _request_client(request, body_client_id)
    offboarded = _offboarded_client_response(client)
    if offboarded:
        return offboarded

    files_list = []
    edi_text = ""
    original_filename = "uploaded_file.x12"

    if request.content_type == "application/json":
        if body.get("files") and isinstance(body["files"], list):
            files_list = body["files"]
        else:
            edi_text = body.get("edi_text", "")
            original_filename = body.get("original_filename", "pasted_file.x12")
    else:
        file_objs = request.FILES.getlist("edi_files") or request.FILES.getlist("edi_file")
        if file_objs and len(file_objs) > 1:
            for file_obj in file_objs:
                invalid = _invalid_835_response(file_obj.name)
                if invalid:
                    return invalid
                files_list.append({
                    "filename": file_obj.name,
                    "content": file_obj.read().decode("utf-8", errors="ignore"),
                })
        elif file_objs:
            original_filename = file_objs[0].name
            invalid = _invalid_835_response(original_filename)
            if invalid:
                return invalid
            edi_text = file_objs[0].read().decode("utf-8", errors="ignore")
        else:
            edi_text = request.POST.get("edi_text", "")
            original_filename = request.POST.get("original_filename", "pasted_file.x12")

    if files_list:
        invalid = _invalid_835_batch_response(files_list)
        if invalid:
            return invalid
        total_claims = 0
        total_errors = []
        valid_files_count = 0
        for item in files_list:
            filename = item.get("filename") or item.get("original_filename") or "file.835"
            content = (item.get("content") or item.get("edi_text") or "").strip()
            if not content:
                continue
            report = _validate_835_for_conversion(content)
            is_valid = report.get("valid", report.get("is_valid", True))
            claims = report.get("claims", report.get("claims_found", 0))
            total_claims += claims
            if is_valid:
                valid_files_count += 1
            else:
                errors = report.get("errors", [])
                total_errors.append(
                    f"{filename}: {', '.join(str(error) for error in errors) if errors else 'Validation failed'}"
                )

        aggregated_report = {
            "valid": not total_errors,
            "is_valid": not total_errors,
            "claims": total_claims,
            "claims_found": total_claims,
            "valid_files_count": valid_files_count,
            "total_files_count": len(files_list),
            "errors": total_errors,
        }
        if client:
            try:
                _send_validation_notice(
                    client,
                    request,
                    [item.get("filename") or item.get("original_filename") or "file.835" for item in files_list],
                    not total_errors,
                    total_claims,
                    total_errors,
                )
            except Exception as exc:
                logger.error("Failed to send validation email: %s", exc)
        return JsonResponse({
            "success": True,
            "report": aggregated_report,
            "is_valid": not total_errors,
            "files_count": len(files_list),
        })

    edi_text = edi_text.strip()
    if not edi_text:
        return JsonResponse({"error": "Please provide EDI content to validate."}, status=400)

    try:
        from pathlib import Path
        from edi835.services import get_edi835_storage_dirs

        dirs = get_edi835_storage_dirs(client)
        archive_file_path = dirs["archive"] / original_filename
        archive_file_path.write_text(edi_text, encoding="utf-8")
        rel_archive_path = (Path("media") / "edi835" / "archive" / original_filename).as_posix()
        report = _validate_835_for_conversion(edi_text)
        is_valid = report.get("valid", report.get("is_valid", True))
        claims_found = report.get("claims", report.get("claims_found", 0))
        report["is_valid"] = is_valid
        report["claims_found"] = claims_found

        if is_valid:
            db_record = EDI835File.objects.create(
                original_filename=original_filename,
                stored_filename=original_filename,
                input_file_content=edi_text,
                status="PROCESSING",
                claims_count=claims_found,
                archive_path=rel_archive_path,
                input_path=rel_archive_path,
                present_in_archive_folder=True,
                client=client,
            )
        else:
            error_message = json.dumps({
                "message": "835 validation failed",
                "errors": report.get("errors", ["Validation errors found"]),
                "findings": report.get("findings", []),
                "validator_engine": report.get("validator_engine", "OneSmarter 835 structural validation"),
            })
            db_record = EDI835File.objects.create(
                original_filename=original_filename,
                stored_filename=original_filename,
                input_file_content=edi_text,
                status="ERROR",
                claims_count=claims_found,
                error_message=error_message,
                archive_path=rel_archive_path,
                input_path=rel_archive_path,
                present_in_archive_folder=True,
                client=client,
            )

        if client:
            try:
                _send_validation_notice(
                    client,
                    request,
                    [original_filename],
                    is_valid,
                    claims_found,
                    report.get("errors", []),
                )
            except Exception as exc:
                logger.error("Failed to send validation email: %s", exc)
        return JsonResponse({"success": True, "file_id": str(db_record.id), "report": report})
    except Exception as exc:
        logger.exception("Local validation error for file %s", original_filename)
        db_record = EDI835File.objects.create(
            original_filename=original_filename,
            stored_filename=original_filename,
            input_file_content=edi_text,
            status="ERROR",
            error_message=str(exc),
            client=client,
        )
        try:
            _send_validation_notice(client, request, [original_filename], False, 0, [str(exc)])
        except Exception as email_exc:
            logger.error("Failed to send validation failure email: %s", email_exc)
        return JsonResponse({
            "error": f"Local validation error: {exc}",
            "file_id": str(db_record.id),
        }, status=400)
