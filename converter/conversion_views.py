import json
import logging
import os

from django.http import JsonResponse

from edi835.models import EDI835File
from edi835.services import process_edi835_file_content, process_multiple_edi835_files

from .views import (
    _canonical_mir_filename,
    _invalid_835_response,
    _offboarded_client_response,
    _request_client,
)


# 2026-09-23 - Yash: Removed csrf_exempt decorator for CSRF protection
def api_convert(request):
    """Convert one or more validated 835 files while preserving the established response contract."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Only POST method is allowed.'}, status=405)

    files_list = []
    edi_text = ''
    original_filename = 'uploaded_file.x12'
    file_id = None

    if request.content_type == 'application/json':
        try:
            body = json.loads(request.body.decode('utf-8'))
            if body.get('files') and isinstance(body['files'], list) and body['files']:
                files_list = body['files']
            else:
                edi_text = body.get('edi_text', '')
                original_filename = body.get('original_filename', 'pasted_file.x12')
                file_id = body.get('file_id')
        except Exception:
            body = {}
    else:
        body = {}
        file_objs = request.FILES.getlist('edi_files') or request.FILES.getlist('edi_file')
        if file_objs and len(file_objs) > 1:
            for file_obj in file_objs:
                try:
                    files_list.append({
                        'filename': file_obj.name,
                        'content': file_obj.read().decode('utf-8', errors='ignore'),
                    })
                except Exception:
                    pass
        elif file_objs:
            original_filename = file_objs[0].name
            invalid = _invalid_835_response(original_filename)
            if invalid:
                return invalid
            try:
                edi_text = file_objs[0].read().decode('utf-8', errors='ignore')
            except Exception as exc:
                return JsonResponse({'error': f'Failed to read uploaded file: {exc}'}, status=400)
        else:
            edi_text = request.POST.get('edi_text', '')
            original_filename = request.POST.get('original_filename', 'pasted_file.x12')
            file_id = request.POST.get('file_id')

    body_client_id = (
        body.get('client_id') or body.get('client')
        if request.content_type == 'application/json'
        else request.POST.get('client_id') or request.POST.get('client')
    )
    client = _request_client(request, body_client_id)
    offboarded = _offboarded_client_response(client)
    if offboarded:
        return offboarded

    if files_list:
        batch_res = process_multiple_edi835_files(files_list, client=client)
        if not batch_res.get('success'):
            if client:
                try:
                    from admin_panel.email_service import send_batch_validation_refusal_notice, send_conversion_notice
                    error_message = batch_res.get('error', 'Multi-file conversion failed.')
                    send_batch_validation_refusal_notice(
                        client,
                        request,
                        refused_files=batch_res.get('refused_files', []),
                        accepted_files=batch_res.get('accepted_files', []),
                    )
                    send_conversion_notice(
                        client,
                        request,
                        success=False,
                        batch=True,
                        input_files=[item.get('filename') or item.get('original_filename') or 'file.835' for item in files_list],
                        error=error_message,
                    )
                except Exception as exc:
                    logging.getLogger(__name__).error('Failed to send batch failure email: %s', exc)
            failed_record = batch_res.get('db_record')
            return JsonResponse({
                'error': batch_res.get('error', 'Multi-file conversion failed.'),
                'partial': batch_res.get('partial', False),
                'file_id': str(failed_record.id) if failed_record else None,
                'output_path': getattr(failed_record, 'output_path', '') if failed_record else '',
                'delivered_claims_count': getattr(failed_record, 'delivered_claims_count', 0) if failed_record else 0,
                'held_claims_count': getattr(failed_record, 'held_claims_count', 0) if failed_record else 0,
                'findings': batch_res.get('findings', []),
            }, status=400)

        primary_record = batch_res.get('db_record')
        canonical_mir_filename = _canonical_mir_filename(primary_record)
        if client:
            try:
                from admin_panel.email_service import send_batch_validation_refusal_notice, send_conversion_notice
                send_batch_validation_refusal_notice(
                    client,
                    request,
                    refused_files=batch_res.get('refused_files', []),
                    accepted_files=batch_res.get('accepted_files', []),
                    output_files=[canonical_mir_filename or batch_res.get('combined_filename')],
                )
                send_conversion_notice(
                    client,
                    request,
                    success=True,
                    batch=True,
                    input_files=batch_res.get('accepted_files', []),
                    output_files=[canonical_mir_filename or batch_res.get('combined_filename')],
                    claims=batch_res.get('claims_count', 0),
                    services=batch_res.get('services_count', 0),
                    records=batch_res.get('records_count', 0),
                )
            except Exception as exc:
                logging.getLogger(__name__).error('Failed to send email: %s', exc)

        user_name = 'System'
        if request.user and request.user.is_authenticated:
            user_name = request.user.name or request.user.email
        from admin_panel.models import log_audit_event
        log_audit_event(
            module='DOCUMENTS',
            action='BATCH_CONVERSION',
            details=f"Batch converted {batch_res['files_count']} EDI 835 files. Claims: {batch_res['claims_count']}.",
            performed_by=user_name,
            client=client,
        )
        return JsonResponse({
            'success': True,
            'text': batch_res['mir_text'],
            'files_count': batch_res['files_count'],
            'claims_count': batch_res['claims_count'],
            'services_count': batch_res['services_count'],
            'records_count': batch_res['records_count'],
            'file_id': str(primary_record.id) if primary_record else None,
            'combined_filename': canonical_mir_filename or batch_res.get('combined_filename'),
            'mir_filename': canonical_mir_filename or batch_res.get('combined_filename'),
            'sftp_uploaded': batch_res.get('sftp_uploaded', False),
            'errors': batch_res.get('errors', []),
            'accepted_files': batch_res.get('accepted_files', []),
            'refused_files': batch_res.get('refused_files', []),
            'partial': batch_res.get('partial', False),
            'delivered_claims_count': getattr(primary_record, 'delivered_claims_count', 0),
            'held_claims_count': getattr(primary_record, 'held_claims_count', 0),
            'findings': batch_res.get('findings', []),
            'output_path': getattr(primary_record, 'output_path', ''),
        })

    edi_text = edi_text.strip()
    if not edi_text and file_id:
        try:
            from pathlib import Path
            from django.conf import settings
            from edi835.services import get_edi835_storage_dirs

            record = EDI835File.objects.get(id=file_id)
            if record.original_filename:
                original_filename = record.original_filename
            directories = get_edi835_storage_dirs(record.client)
            possible_paths = []
            if record.input_path:
                possible_paths.append(Path(settings.BASE_DIR) / record.input_path)
            if record.archive_path:
                possible_paths.append(Path(settings.BASE_DIR) / record.archive_path)
            if record.stored_filename:
                possible_paths.extend([
                    directories['input'] / record.stored_filename,
                    directories['processing'] / record.stored_filename,
                    directories['archive'] / record.stored_filename,
                ])
            for path in possible_paths:
                if os.path.exists(path) and os.path.isfile(path):
                    content = path.read_text(encoding='utf-8', errors='ignore').strip()
                    if content:
                        edi_text = content
                        break
        except Exception:
            pass

    if not edi_text:
        return JsonResponse({'error': 'Please provide EDI 835 text or upload file(s).'}, status=400)

    result = process_edi835_file_content(
        edi_text,
        original_filename=original_filename,
        file_id=file_id,
        client=client,
    )
    if not result.get('success'):
        if client:
            try:
                from admin_panel.email_service import send_conversion_notice
                error_message = result.get('error', 'Unknown error')
                send_conversion_notice(
                    client,
                    request,
                    success=False,
                    input_files=[original_filename],
                    error=error_message,
                )
            except Exception as exc:
                logging.getLogger(__name__).error('Failed to send conversion failure email: %s', exc)
        db_record = result.get('db_record')
        return JsonResponse({
            'error': f"Failed to convert EDI file: {result.get('error')}",
            'partial': result.get('partial', False),
            'file_id': str(db_record.id) if db_record else None,
            'output_path': getattr(db_record, 'output_path', ''),
            'delivered_claims_count': getattr(db_record, 'delivered_claims_count', 0),
            'held_claims_count': getattr(db_record, 'held_claims_count', 0),
            'findings': result.get('findings', []),
        }, status=400)

    db_record = result.get('db_record')
    if client:
        try:
            from admin_panel.email_service import send_conversion_notice
            send_conversion_notice(
                client,
                request,
                success=True,
                input_files=[original_filename],
                output_files=[_canonical_mir_filename(db_record)],
                claims=result.get('claims_count', 0),
                services=result.get('services_count', 0),
                records=result.get('records_count', 0),
            )
        except Exception as exc:
            logging.getLogger(__name__).error('Failed to send email: %s', exc)

    user_name = 'System'
    if request.user and request.user.is_authenticated:
        user_name = request.user.name or request.user.email
    from admin_panel.models import log_audit_event
    log_audit_event(
        module='DOCUMENTS',
        action='FILE_CONVERSION',
        details=f"Converted EDI 835 file '{original_filename}'. Claims: {result['claims_count']}.",
        performed_by=user_name,
        client=client,
    )

    mir_filename = _canonical_mir_filename(db_record)
    return JsonResponse({
        'success': True,
        'text': result['mir_text'],
        'claims_count': result['claims_count'],
        'services_count': result['services_count'],
        'records_count': result['records_count'],
        'file_id': str(db_record.id),
        'output_path': db_record.output_path,
        'archive_path': db_record.archive_path,
        'mir_filename': mir_filename,
        'filename': mir_filename,
        'partial': result.get('partial', False),
        'delivered_claims_count': db_record.delivered_claims_count,
        'held_claims_count': db_record.held_claims_count,
        'findings': result.get('findings', []),
    })
