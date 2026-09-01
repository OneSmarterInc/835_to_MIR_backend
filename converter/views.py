import json
import os
import logging
from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger("converter")

from converter.services.parser import parse_835_to_mir
from converter.services.validator import EDI835Validator
from edi835.models import EDI835File
from edi835.services import process_edi835_file_content, process_multiple_edi835_files
from edi835.file_types import file_extension_error, has_valid_file_extension


def _invalid_835_response(filename):
    if has_valid_file_extension(filename, "835"):
        return None
    return JsonResponse({'success': False, 'error': file_extension_error("835")}, status=400)


def _invalid_835_batch_response(files):
    for item in files or []:
        invalid = _invalid_835_response(item.get('filename') or item.get('original_filename') or '')
        if invalid:
            return invalid
    return None


def _request_client(request, requested_client_id=None):
    """Resolve tenant scope without letting a client user impersonate another tenant."""
    user = getattr(request, "user", None)
    if user and user.is_authenticated and not user.is_staff:
        return getattr(user, "client", None)
    if requested_client_id:
        from accounts.models import Client
        try:
            return Client.objects.get(id=requested_client_id)
        except (Client.DoesNotExist, ValueError):
            return None
    return getattr(user, "client", None) if user and user.is_authenticated else None


def _canonical_mir_filename(record):
    """Return the persisted admin-configured MIR filename for a conversion record."""
    if not record:
        return ""
    mir_record = getattr(record, "mir_file", None)
    if mir_record and mir_record.mir_filename:
        return os.path.basename(mir_record.mir_filename)
    return ""


def _safe_mir_filename(value, fallback="output.mir"):
    """Normalize a client-facing MIR download filename without trusting paths."""
    filename = os.path.basename(str(value or "").strip())
    if not filename:
        filename = fallback
    if not filename.lower().endswith(".mir"):
        filename += ".mir"
    return filename


@csrf_exempt
def api_convert(request):
    """
    API Endpoint: Convert EDI 835 text or uploaded file(s) to MIR format.
    Supports single file or multiple 835 files converted into a SINGLE MIR output file.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method is allowed.'}, status=405)

    files_list = []
    edi_text = ""
    original_filename = "uploaded_file.x12"
    file_id = None

    # Check for multiple files in JSON body
    if request.content_type == 'application/json':
        try:
            body = json.loads(request.body.decode('utf-8'))
            if body.get('files') and isinstance(body['files'], list) and len(body['files']) > 0:
                files_list = body['files']
            else:
                edi_text = body.get('edi_text', '')
                original_filename = body.get('original_filename', 'pasted_file.x12')
                file_id = body.get('file_id')
        except Exception:
            edi_text = ''
    else:
        file_objs = request.FILES.getlist('edi_files') or request.FILES.getlist('edi_file')
        if file_objs and len(file_objs) > 1:
            for fobj in file_objs:
                invalid = _invalid_835_response(fobj.name)
                if invalid:
                    return invalid
                try:
                    content = fobj.read().decode('utf-8', errors='ignore')
                    files_list.append({'filename': fobj.name, 'content': content})
                except Exception:
                    pass
        elif file_objs:
            original_filename = file_objs[0].name
            invalid = _invalid_835_response(original_filename)
            if invalid:
                return invalid
            try:
                edi_text = file_objs[0].read().decode('utf-8', errors='ignore')
            except Exception as e:
                return JsonResponse({'error': f'Failed to read uploaded file: {str(e)}'}, status=400)
        else:
            edi_text = request.POST.get('edi_text', '')
            original_filename = request.POST.get('original_filename', 'pasted_file.x12')
            file_id = request.POST.get('file_id')

    client = None
    body_client_id = None
    if request.content_type == 'application/json':
        try:
            body = json.loads(request.body.decode('utf-8'))
            body_client_id = body.get('client_id') or body.get('client')
        except Exception:
            pass
    else:
        body_client_id = request.POST.get('client_id') or request.POST.get('client')

    client = _request_client(request, body_client_id)

    if files_list and len(files_list) > 0:
        invalid = _invalid_835_batch_response(files_list)
        if invalid:
            return invalid
        batch_res = process_multiple_edi835_files(files_list, client=client)
        if not batch_res.get("success"):
            if client:
                try:
                    from admin_panel.email_service import send_client_email
                    err_msg = batch_res.get("error", "Multi-file conversion failed.")
                    subject = f"OneSmarter: Batch 835 Conversion Failed"
                    html = (
                        f"<h3>Batch 835 Conversion Failed</h3>"
                        f"<p>Your batch of EDI 835 files could not be converted to MIR.</p>"
                        f"<p><b>Error:</b> {err_msg}</p>"
                        f"<p>Please review the files and try again.</p>"
                    )
                    to_emails = [request.user.email] if request.user and request.user.email else None
                    send_client_email(client, subject, html, to_emails=to_emails)
                except Exception as email_err:
                    logging.getLogger(__name__).error(f"Failed to send batch failure email: {email_err}")
            return JsonResponse({'error': batch_res.get("error", "Multi-file conversion failed.")}, status=400)

        primary_rec = batch_res.get("db_record")
        canonical_mir_filename = _canonical_mir_filename(primary_rec)

        if client:
            try:
                from admin_panel.email_service import send_client_email
                subject = f"OneSmarter: Batch 835 Conversion Successful"
                html = f"<h3>Batch File Conversion Successful</h3><p>Your batch of {batch_res['files_count']} EDI 835 files was successfully converted to MIR.</p><p>Total claims processed: {batch_res['claims_count']}</p>"
                to_emails = [request.user.email] if request.user and request.user.email else None
                send_client_email(client, subject, html, to_emails=to_emails)
            except Exception as e:
                logging.getLogger(__name__).error(f"Failed to send email: {e}")

        user_name = "System"
        if request.user and request.user.is_authenticated:
            user_name = request.user.name or request.user.email
        from admin_panel.models import log_audit_event
        log_audit_event(
            module="DOCUMENTS",
            action="BATCH_CONVERSION",
            details=f"Batch converted {batch_res['files_count']} EDI 835 files. Claims: {batch_res['claims_count']}.",
            performed_by=user_name,
            client=client
        )

        return JsonResponse({
            'success': True,
            'text': batch_res['mir_text'],
            'files_count': batch_res['files_count'],
            'claims_count': batch_res['claims_count'],
            'services_count': batch_res['services_count'],
            'records_count': batch_res['records_count'],
            'file_id': str(primary_rec.id) if primary_rec else None,
            'combined_filename': canonical_mir_filename or batch_res.get('combined_filename'),
            'mir_filename': canonical_mir_filename or batch_res.get('combined_filename'),
            'sftp_uploaded': batch_res.get('sftp_uploaded', False),
            'errors': batch_res.get('errors', []),
        })

    edi_text = edi_text.strip()
    if not edi_text and file_id:
        try:
            from pathlib import Path
            from django.conf import settings
            from edi835.services import get_edi835_storage_dirs
            rec = EDI835File.objects.get(id=file_id)
            if rec.original_filename:
                original_filename = rec.original_filename
            dirs = get_edi835_storage_dirs()
            possible_paths = []
            if rec.input_path:
                possible_paths.append(Path(settings.BASE_DIR) / rec.input_path)
            if rec.archive_path:
                possible_paths.append(Path(settings.BASE_DIR) / rec.archive_path)
            if rec.stored_filename:
                possible_paths.append(dirs["input"] / rec.stored_filename)
                possible_paths.append(dirs["processing"] / rec.stored_filename)
                possible_paths.append(dirs["archive"] / rec.stored_filename)

            for p in possible_paths:
                if os.path.exists(p) and os.path.isfile(p):
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read().strip()
                    if content:
                        edi_text = content
                        break
        except Exception:
            pass

    if not edi_text:
        return JsonResponse({'error': 'Please provide EDI 835 text or upload file(s).'}, status=400)

    res = process_edi835_file_content(edi_text, original_filename=original_filename, file_id=file_id, client=client)

    if not res.get("success"):
        if client:
            try:
                from admin_panel.email_service import send_client_email
                err_msg = res.get("error", "Unknown error")
                subject = f"OneSmarter: 835 File Conversion Failed - {original_filename}"
                html = (
                    f"<h3>835 File Conversion Failed</h3>"
                    f"<p>Your EDI 835 file <b>{original_filename}</b> could not be converted to MIR.</p>"
                    f"<p><b>Error:</b> {err_msg}</p>"
                    f"<p>Please review the file and re-upload a corrected version.</p>"
                )
                to_emails = [request.user.email] if request.user and request.user.email else None
                send_client_email(client, subject, html, to_emails=to_emails)
            except Exception as email_err:
                logging.getLogger(__name__).error(f"Failed to send conversion failure email: {email_err}")
        return JsonResponse({
            'error': f'Failed to convert EDI file: {res.get("error")}',
            'file_id': str(res["db_record"].id) if res.get("db_record") else None
        }, status=400)

    if client:
        try:
            from admin_panel.email_service import send_client_email
            subject = f"OneSmarter: 835 Conversion Successful - {original_filename}"
            html = f"<h3>File Conversion Successful</h3><p>Your EDI 835 file <b>{original_filename}</b> was successfully converted to MIR.</p><p>Claims processed: {res['claims_count']}</p>"
            to_emails = [request.user.email] if request.user and request.user.email else None
            send_client_email(client, subject, html, to_emails=to_emails)
        except Exception as e:
            logging.getLogger(__name__).error(f"Failed to send email: {e}")

    user_name = "System"
    if request.user and request.user.is_authenticated:
        user_name = request.user.name or request.user.email
    from admin_panel.models import log_audit_event
    log_audit_event(
        module="DOCUMENTS",
        action="FILE_CONVERSION",
        details=f"Converted EDI 835 file '{original_filename}'. Claims: {res['claims_count']}.",
        performed_by=user_name,
        client=client
    )

    mir_filename = _canonical_mir_filename(res.get("db_record"))

    return JsonResponse({
        'success': True,
        'text': res['mir_text'],
        'claims_count': res['claims_count'],
        'services_count': res['services_count'],
        'records_count': res['records_count'],
        'file_id': str(res['db_record'].id),
        'output_path': res['db_record'].output_path,
        'archive_path': res['db_record'].archive_path,
        'mir_filename': mir_filename,
        'filename': mir_filename,
    })


@csrf_exempt
def api_validate(request):
    """
    API Endpoint: Validate EDI 835 files using Local X12/835 PyX12 Engine.
    Supports single or multi-file validation.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method is allowed.'}, status=405)

    client = None
    body_client_id = None
    if request.content_type == 'application/json':
        try:
            body = json.loads(request.body.decode('utf-8'))
            body_client_id = body.get('client_id') or body.get('client')
        except Exception:
            pass
    else:
        body_client_id = request.POST.get('client_id') or request.POST.get('client')

    client = _request_client(request, body_client_id)

    files_list = []
    edi_text = ""
    original_filename = "uploaded_file.x12"

    if request.content_type == 'application/json':
        try:
            body = json.loads(request.body.decode('utf-8'))
            if body.get('files') and isinstance(body['files'], list) and len(body['files']) > 0:
                files_list = body['files']
            else:
                edi_text = body.get('edi_text', '')
                original_filename = body.get('original_filename', 'pasted_file.x12')
        except Exception:
            edi_text = ''
    else:
        file_objs = request.FILES.getlist('edi_files') or request.FILES.getlist('edi_file')
        if file_objs and len(file_objs) > 1:
            for fobj in file_objs:
                invalid = _invalid_835_response(fobj.name)
                if invalid:
                    return invalid
                try:
                    content = fobj.read().decode('utf-8', errors='ignore')
                    files_list.append({'filename': fobj.name, 'content': content})
                except Exception:
                    pass
        elif file_objs:
            original_filename = file_objs[0].name
            invalid = _invalid_835_response(original_filename)
            if invalid:
                return invalid
            try:
                edi_text = file_objs[0].read().decode('utf-8', errors='ignore')
            except Exception as e:
                return JsonResponse({'error': f'Failed to read uploaded file: {str(e)}'}, status=400)
        else:
            edi_text = request.POST.get('edi_text', '')
            original_filename = request.POST.get('original_filename', 'pasted_file.x12')

    if files_list and len(files_list) > 0:
        invalid = _invalid_835_batch_response(files_list)
        if invalid:
            return invalid
        total_claims = 0
        total_errors = []
        valid_files_count = 0
        validator = EDI835Validator()

        for item in files_list:
            fname = item.get('filename') or item.get('original_filename') or 'file.835'
            content = (item.get('content') or item.get('edi_text') or '').strip()
            if not content:
                continue

            report = validator.validate(content)
            is_val = report.get('valid', report.get('is_valid', True))
            claims = report.get('claims', report.get('claims_found', 0))
            total_claims += claims

            if is_val:
                valid_files_count += 1
            else:
                errs = report.get('errors', [])
                total_errors.append(f"{fname}: {', '.join([str(e) for e in errs]) if errs else 'Validation failed'}")

        aggregated_report = {
            'valid': len(total_errors) == 0,
            'is_valid': len(total_errors) == 0,
            'claims': total_claims,
            'claims_found': total_claims,
            'valid_files_count': valid_files_count,
            'total_files_count': len(files_list),
            'errors': total_errors,
        }

        if client:
            try:
                from admin_panel.email_service import send_client_email
                subject = f"OneSmarter: Batch 835 Validation Completed"
                status_str = "Valid" if len(total_errors) == 0 else "Invalid (errors found)"
                html = f"<h3>Batch File Validation Completed</h3><p>Your batch of {len(files_list)} EDI 835 files validation result: <b>{status_str}</b>.</p><p>Total claims: {total_claims}</p>"
                to_emails = [request.user.email] if request.user and request.user.email else None
                send_client_email(client, subject, html, to_emails=to_emails)
            except Exception as e:
                logging.getLogger(__name__).error(f"Failed to send email: {e}")

        return JsonResponse({
            'success': True,
            'report': aggregated_report,
            'is_valid': len(total_errors) == 0,
            'files_count': len(files_list)
        })

    edi_text = edi_text.strip()
    if not edi_text:
        return JsonResponse({'error': 'Please provide EDI content to validate.'}, status=400)

    try:
        from pathlib import Path
        from django.conf import settings
        from edi835.services import get_edi835_storage_dirs

        dirs = get_edi835_storage_dirs()
        archive_file_path = dirs["archive"] / original_filename
        with open(archive_file_path, "w", encoding="utf-8") as f:
            f.write(edi_text)
        rel_archive_path = (Path("media") / "edi835" / "archive" / original_filename).as_posix()

        validator = EDI835Validator()
        report = validator.validate(edi_text)

        is_valid = report.get('valid', report.get('is_valid', True))
        claims_found = report.get('claims', report.get('claims_found', 0))

        report['is_valid'] = is_valid
        report['claims_found'] = claims_found

        if is_valid:
            db_rec = EDI835File.objects.create(
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
            err_msg = json.dumps(report.get("errors", ["Validation errors found"]))
            db_rec = EDI835File.objects.create(
                original_filename=original_filename,
                stored_filename=original_filename,
                input_file_content=edi_text,
                status="ERROR",
                claims_count=claims_found,
                error_message=err_msg,
                archive_path=rel_archive_path,
                input_path=rel_archive_path,
                present_in_archive_folder=True,
                client=client,
            )

        if client:
            try:
                from admin_panel.email_service import send_client_email
                subject = f"OneSmarter: 835 Validation Completed - {original_filename}"
                status_str = "Valid" if is_valid else "Invalid"
                html = f"<h3>File Validation Completed</h3><p>Your EDI 835 file <b>{original_filename}</b> validation result: <b>{status_str}</b>.</p><p>Claims found: {claims_found}</p>"
                to_emails = [request.user.email] if request.user and request.user.email else None
                send_client_email(client, subject, html, to_emails=to_emails)
            except Exception as e:
                logging.getLogger(__name__).error(f"Failed to send email: {e}")

        return JsonResponse({
            'success': True,
            'file_id': str(db_rec.id),
            'report': report
        })
    except Exception as err:
        logger.exception(f"Local validation error for file '{original_filename}': {str(err)}")
        db_rec = EDI835File.objects.create(
            original_filename=original_filename,
            stored_filename=original_filename,
            input_file_content=edi_text,
            status="ERROR",
            error_message=str(err),
            client=client,
        )
        return JsonResponse({
            'error': f'Local validation error: {str(err)}',
            'file_id': str(db_rec.id)
        }, status=400)


@csrf_exempt
def download_mir(request):
    """Download an MIR using the canonical filename persisted for the conversion record."""
    if request.method == 'POST':
        mir_content = request.POST.get('mir_content', '')
        requested_name = request.POST.get('file_name', '')
        file_id = request.POST.get('file_id')
    else:
        mir_content = request.GET.get('mir_content', '')
        requested_name = request.GET.get('file_name', '')
        file_id = request.GET.get('file_id')

    rec = None
    if file_id:
        try:
            rec = EDI835File.objects.select_related("mir_file").filter(id=file_id).first()
        except (ValueError, TypeError):
            rec = None

    canonical_name = _canonical_mir_filename(rec)
    file_name = _safe_mir_filename(canonical_name or requested_name or "output.mir")

    if not mir_content:
        try:
            from edi835.services import get_edi835_storage_dirs
            from pathlib import Path
            from django.conf import settings

            dirs = get_edi835_storage_dirs()

            if rec and getattr(rec, "mir_file", None):
                mir_content = rec.mir_file.file_content or ""

            if not mir_content and rec and rec.output_path:
                abs_p = Path(settings.BASE_DIR) / rec.output_path
                if os.path.exists(abs_p):
                    with open(abs_p, "r", encoding="utf-8", errors="ignore") as f:
                        mir_content = f.read()

            # Backward-compatible physical lookup: the disk name can be tenant-prefixed,
            # while the response/download name is always the persisted canonical name.
            if not mir_content and rec:
                physical_name = os.path.basename(rec.output_path or "")
                if physical_name:
                    out_p = dirs["output"] / physical_name
                    if os.path.exists(out_p):
                        with open(out_p, "r", encoding="utf-8", errors="ignore") as f:
                            mir_content = f.read()

            if not mir_content and file_name:
                out_p = dirs["output"] / file_name
                if os.path.exists(out_p):
                    with open(out_p, "r", encoding="utf-8", errors="ignore") as f:
                        mir_content = f.read()
        except Exception as e:
            logger.warning("MIR download lookup failed: %s", e)

    if mir_content:
        lines = [l.strip() for l in mir_content.splitlines() if l and l.strip()]
        mir_content = "\n".join(lines) + ("\n" if lines else "")

    response = HttpResponse(mir_content, content_type='text/plain')
    response['Content-Disposition'] = f'attachment; filename="{file_name}"'
    response['X-OneSmarter-Filename'] = file_name
    return response


@csrf_exempt
def api_download_archive_zip(request):
    """
    Create a ZIP from database-backed content.
    type parameter: 'mir' | '835' | 'recon' | 'both' | 'all'
    """
    import io
    import zipfile
    from pathlib import PurePath
    from edi835.models import EDI835File, MIRFile, RECONFile

    download_type = (request.GET.get("type") or "both").lower()
    client_id = request.GET.get("client")
    if download_type not in {"mir", "835", "recon", "both", "all"}:
        return JsonResponse({"error": "Invalid archive type."}, status=400)

    mem_zip = io.BytesIO()
    added_paths = set()

    def add_text(zf, folder, filename, content, record_id):
        if content is None or content == "":
            return
        filename = PurePath(filename or "").name or str(record_id)
        archive_path = f"{folder}/{filename}"
        if archive_path in added_paths:
            stem, extension = os.path.splitext(filename)
            archive_path = f"{folder}/{stem}_{str(record_id)[:8]}{extension}"
        zf.writestr(archive_path, content)
        added_paths.add(archive_path)

    with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        client_filter = {"client_id": client_id} if client_id else {}

        if download_type in {"835", "both", "all"}:
            for record in EDI835File.objects.filter(**client_filter).only(
                "id", "original_filename", "stored_filename", "input_file_content"
            ).iterator():
                add_text(zf, "835", record.original_filename or record.stored_filename,
                         record.input_file_content, record.id)

        if download_type in {"mir", "both", "all"}:
            for record in MIRFile.objects.filter(**client_filter).only(
                "id", "mir_filename", "file_content"
            ).iterator():
                add_text(zf, "MIR", record.mir_filename, record.file_content, record.id)

        if download_type in {"recon", "all"}:
            for record in RECONFile.objects.filter(**client_filter).only(
                "id", "original_filename", "stored_filename", "file_content"
            ).iterator():
                add_text(zf, "RECON", record.original_filename or record.stored_filename,
                         record.file_content, record.id)

    mem_zip.seek(0)
    if not added_paths:
        return JsonResponse({"error": f"No {download_type} files found to archive."}, status=404)

    response = HttpResponse(mem_zip.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="edi835_{download_type}_archive.zip"'
    return response


def api_get_file_content(request, file_id):
    """
    Fetch preview content exclusively from the persisted 835 and MIR tables.

    Preview requests deliberately never read or parse physical files.
    """
    try:
        db_rec = EDI835File.objects.select_related("mir_file").get(id=file_id)
    except (EDI835File.DoesNotExist, ValueError):
        return JsonResponse({"error": "File record not found."}, status=404)

    if getattr(request.user, "client", None) != db_rec.client and not request.user.is_staff:
        return JsonResponse({"error": "Unauthorized access to file."}, status=403)

    edi_text = db_rec.input_file_content or ""
    mir_record = getattr(db_rec, "mir_file", None)
    mir_text = mir_record.file_content if mir_record else ""

    return JsonResponse({
        "success": True,
        "file_id": str(db_rec.id),
        "filename": db_rec.original_filename,
        "mir_filename": mir_record.mir_filename if mir_record else "",
        "edi_text": edi_text,
        "mir_text": mir_text,
    })
