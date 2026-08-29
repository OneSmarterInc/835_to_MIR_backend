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


@csrf_exempt
def api_convert(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method is allowed.'}, status=405)
    files_list = []
    edi_text = ""
    original_filename = "uploaded_file.x12"
    file_id = None
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
                try:
                    content = fobj.read().decode('utf-8', errors='ignore')
                    files_list.append({'filename': fobj.name, 'content': content})
                except Exception:
                    pass
        elif file_objs:
            original_filename = file_objs[0].name
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

    if body_client_id:
        from accounts.models import Client
        try:
            client = Client.objects.get(id=body_client_id)
        except (Client.DoesNotExist, ValueError):
            pass
    if not client and request.user and request.user.is_authenticated:
        client = getattr(request.user, "client", None)

    if files_list and len(files_list) > 0:
        batch_res = process_multiple_edi835_files(files_list, client=client)
        if not batch_res.get("success"):
            return JsonResponse({'error': batch_res.get("error", "Multi-file conversion failed.")}, status=400)
        primary_rec = batch_res.get("db_record")
        from admin_panel.models import log_audit_event
        user_name = "System"
        if request.user and request.user.is_authenticated:
            user_name = request.user.name or request.user.email
        log_audit_event(module="DOCUMENTS", action="BATCH_CONVERSION", details=f"Batch converted {batch_res['files_count']} EDI 835 files. Claims: {batch_res['claims_count']}.", performed_by=user_name, client=client)
        return JsonResponse({
            'success': True,
            'text': batch_res['mir_text'],
            'files_count': batch_res['files_count'],
            'claims_count': batch_res['claims_count'],
            'services_count': batch_res['services_count'],
            'records_count': batch_res['records_count'],
            'file_id': str(primary_rec.id) if primary_rec else None,
            'combined_filename': batch_res.get('combined_filename'),
            'mir_filename': batch_res.get('combined_filename'),
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
            if rec.input_path: possible_paths.append(Path(settings.BASE_DIR) / rec.input_path)
            if rec.archive_path: possible_paths.append(Path(settings.BASE_DIR) / rec.archive_path)
            if rec.stored_filename:
                possible_paths.extend([dirs["input"] / rec.stored_filename, dirs["processing"] / rec.stored_filename, dirs["archive"] / rec.stored_filename])
            for p in possible_paths:
                if os.path.exists(p) and os.path.isfile(p):
                    with open(p, "r", encoding="utf-8", errors="ignore") as f: content = f.read().strip()
                    if content:
                        edi_text = content
                        break
        except Exception:
            pass
    if not edi_text:
        return JsonResponse({'error': 'Please provide EDI 835 text or upload file(s).'}, status=400)

    res = process_edi835_file_content(edi_text, original_filename=original_filename, file_id=file_id, client=client)
    if not res.get("success"):
        return JsonResponse({'error': f'Failed to convert EDI file: {res.get("error")}', 'file_id': str(res["db_record"].id) if res.get("db_record") else None}, status=400)

    from admin_panel.models import log_audit_event
    user_name = "System"
    if request.user and request.user.is_authenticated:
        user_name = request.user.name or request.user.email
    log_audit_event(module="DOCUMENTS", action="FILE_CONVERSION", details=f"Converted EDI 835 file '{original_filename}'. Claims: {res['claims_count']}.", performed_by=user_name, client=client)

    return JsonResponse({
        'success': True,
        'text': res['mir_text'],
        'claims_count': res['claims_count'],
        'services_count': res['services_count'],
        'records_count': res['records_count'],
        'file_id': str(res['db_record'].id),
        'output_path': res['db_record'].output_path,
        'archive_path': res['db_record'].archive_path,
        'mir_filename': res.get('mir_filename'),
    })


@csrf_exempt
def api_validate(request):
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
    if body_client_id:
        from accounts.models import Client
        try: client = Client.objects.get(id=body_client_id)
        except (Client.DoesNotExist, ValueError): pass
    if not client and request.user and request.user.is_authenticated:
        client = getattr(request.user, "client", None)
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
        except Exception: edi_text = ''
    else:
        file_objs = request.FILES.getlist('edi_files') or request.FILES.getlist('edi_file')
        if file_objs and len(file_objs) > 1:
            for fobj in file_objs:
                try:
                    content = fobj.read().decode('utf-8', errors='ignore')
                    files_list.append({'filename': fobj.name, 'content': content})
                except Exception: pass
        elif file_objs:
            original_filename = file_objs[0].name
            try: edi_text = file_objs[0].read().decode('utf-8', errors='ignore')
            except Exception as e: return JsonResponse({'error': f'Failed to read uploaded file: {str(e)}'}, status=400)
        else:
            edi_text = request.POST.get('edi_text', '')
            original_filename = request.POST.get('original_filename', 'pasted_file.x12')
    if files_list and len(files_list) > 0:
        total_claims = 0; total_errors = []; valid_files_count = 0; validator = EDI835Validator()
        for item in files_list:
            fname = item.get('filename') or item.get('original_filename') or 'file.835'
            content = (item.get('content') or item.get('edi_text') or '').strip()
            if not content: continue
            report = validator.validate(content)
            is_val = report.get('valid', report.get('is_valid', True)); claims = report.get('claims', report.get('claims_found', 0)); total_claims += claims
            if is_val: valid_files_count += 1
            else:
                errs = report.get('errors', [])
                total_errors.append(f"{fname}: {', '.join([str(e) for e in errs]) if errs else 'Validation failed'}")
        aggregated_report = {'valid': len(total_errors) == 0, 'is_valid': len(total_errors) == 0, 'claims': total_claims, 'claims_found': total_claims, 'valid_files_count': valid_files_count, 'total_files_count': len(files_list), 'errors': total_errors}
        return JsonResponse({'success': True, 'report': aggregated_report, 'is_valid': len(total_errors) == 0, 'files_count': len(files_list)})
    edi_text = edi_text.strip()
    if not edi_text: return JsonResponse({'error': 'Please provide EDI content to validate.'}, status=400)
    try:
        from pathlib import Path
        from django.conf import settings
        from edi835.services import get_edi835_storage_dirs
        dirs = get_edi835_storage_dirs()
        archive_file_path = dirs["archive"] / original_filename
        with open(archive_file_path, "w", encoding="utf-8") as f: f.write(edi_text)
        rel_archive_path = (Path("media") / "edi835" / "archive" / original_filename).as_posix()
        validator = EDI835Validator(); report = validator.validate(edi_text)
        is_valid = report.get('valid', report.get('is_valid', True)); claims_found = report.get('claims', report.get('claims_found', 0))
        report['is_valid'] = is_valid; report['claims_found'] = claims_found
        if is_valid:
            db_rec = EDI835File.objects.create(client=client, original_filename=original_filename, stored_filename=original_filename, status="PROCESSING", claims_count=claims_found, archive_path=rel_archive_path, input_path=rel_archive_path, present_in_archive_folder=True)
        else:
            err_msg = json.dumps(report.get("errors", ["Validation errors found"]))
            db_rec = EDI835File.objects.create(client=client, original_filename=original_filename, stored_filename=original_filename, status="ERROR", claims_count=claims_found, error_message=err_msg, archive_path=rel_archive_path, input_path=rel_archive_path, present_in_archive_folder=True)
        return JsonResponse({'success': True, 'file_id': str(db_rec.id), 'report': report})
    except Exception as err:
        logger.exception(f"Local validation error for file '{original_filename}': {str(err)}")
        db_rec = EDI835File.objects.create(client=client, original_filename=original_filename, stored_filename=original_filename, status="ERROR", error_message=str(err))
        return JsonResponse({'error': f'Local validation error: {str(err)}', 'file_id': str(db_rec.id)})


@csrf_exempt
def download_mir(request):
    if request.method == 'POST':
        mir_content = request.POST.get('mir_content', ''); file_name = request.POST.get('file_name', 'output.mir'); file_id = request.POST.get('file_id')
    else:
        mir_content = request.GET.get('mir_content', ''); file_name = request.GET.get('file_name', 'output.mir'); file_id = request.GET.get('file_id')
    if not file_name.endswith('.mir'): file_name += '.mir'
    if not mir_content:
        try:
            from edi835.services import get_edi835_storage_dirs
            from pathlib import Path
            from django.conf import settings
            dirs = get_edi835_storage_dirs(); rec = EDI835File.objects.filter(id=file_id).first() if file_id else None
            if not rec and file_name:
                base_search = file_name.replace("MIR_", "").replace(".mir", "")
                rec = EDI835File.objects.filter(original_filename__icontains=base_search).first()
            if rec and rec.output_path:
                abs_p = Path(settings.BASE_DIR) / rec.output_path
                if os.path.exists(abs_p):
                    with open(abs_p, "r", encoding="utf-8", errors="ignore") as f: mir_content = f.read()
            if not mir_content and file_name:
                out_p = dirs["output"] / file_name
                if os.path.exists(out_p):
                    with open(out_p, "r", encoding="utf-8", errors="ignore") as f: mir_content = f.read()
            if not mir_content and file_name:
                base_p = dirs["output"] / file_name.replace("MIR_", "")
                if os.path.exists(base_p):
                    with open(base_p, "r", encoding="utf-8", errors="ignore") as f: mir_content = f.read()
        except Exception: pass
    if mir_content:
        lines = [l.strip() for l in mir_content.splitlines() if l and l.strip()]
        mir_content = "\n".join(lines) + ("\n" if lines else "")
    response = HttpResponse(mir_content, content_type='text/plain')
    response['Content-Disposition'] = f'attachment; filename="{file_name}"'
    return response
