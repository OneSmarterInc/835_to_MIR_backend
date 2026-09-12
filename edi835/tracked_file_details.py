"""Lazy tracked-file detail endpoints for heavyweight conversion data."""

from django.http import JsonResponse

from admin_panel.access_control import scope_client_queryset

from .models import EDI835File


def _scoped_files(request):
    return scope_client_queryset(
        EDI835File.objects.select_related("client", "mir_file"),
        request.user,
    )


def _is_blocking(finding):
    severity = str((finding or {}).get("severity") or "").upper()
    return severity in {"HOLD", "REFUSE"}


def _claim_key(finding, index):
    finding = finding or {}
    claim_number = finding.get("claim_number") or finding.get("claim_control_number")
    claim_number = str(claim_number or f"held-claim-{index + 1}")
    claim_index = str(finding.get("claim_index") or "").strip()
    return f"{claim_number}:claim-index:{claim_index}" if claim_index else claim_number


def tracked_file_details(request, file_id):
    """Return heavyweight details for one visible tracked file only when requested."""
    record = _scoped_files(request).filter(id=file_id).first()
    if record is None:
        return JsonResponse({"success": False, "error": "Tracked file not found."}, status=404)

    mir_record = getattr(record, "mir_file", None)
    response = JsonResponse(
        {
            "success": True,
            "file": {
                "id": str(record.id),
                "client_id": str(record.client_id) if record.client_id else None,
                "client_name": record.client.name if record.client else "Global System Default",
                "original_filename": record.original_filename,
                "stored_filename": record.stored_filename,
                "mir_filename": mir_record.mir_filename if mir_record and mir_record.mir_filename else "",
                "status": record.status,
                "claims_count": record.claims_count,
                "services_count": record.services_count,
                "records_count": record.records_count,
                "delivered_claims_count": record.delivered_claims_count or 0,
                "held_claims_count": record.held_claims_count or 0,
                "uploaded_at": record.uploaded_at.isoformat() if record.uploaded_at else None,
                "processing_started_at": record.processing_started_at.isoformat() if record.processing_started_at else None,
                "processing_completed_at": record.processing_completed_at.isoformat() if record.processing_completed_at else None,
                "error_message": record.error_message,
                "conversion_findings": record.conversion_findings or [],
                "present_in_sftp": record.present_in_sftp,
                "present_in_archive_folder": record.present_in_archive_folder,
                "ingestion_source": record.ingestion_source or "MANUAL",
            },
        }
    )
    response["Cache-Control"] = "private, no-store, max-age=0"
    return response


def conversion_hold_files(request):
    """Return lightweight file rows for the Conversion Issues tab.

    The JSON findings are read server-side only to derive the number of affected
    claims. They are not sent to the browser until a user opens one file.
    """
    records = (
        _scoped_files(request)
        .defer("input_file_content", "mir_file__file_content")
        .order_by("-uploaded_at")[:200]
    )

    files = []
    for record in records:
        findings = record.conversion_findings or []
        blocking = [finding for finding in findings if _is_blocking(finding)]
        if not blocking and not (record.held_claims_count or 0):
            continue

        issue_keys = {
            _claim_key(finding, index)
            for index, finding in enumerate(blocking)
        }
        issue_count = len(issue_keys) or int(record.held_claims_count or 0)

        files.append(
            {
                "id": str(record.id),
                "client_id": str(record.client_id) if record.client_id else None,
                "client_name": record.client.name if record.client else "Global System Default",
                "original_filename": record.original_filename,
                "stored_filename": record.stored_filename,
                "status": record.status,
                "ingestion_source": record.ingestion_source or "MANUAL",
                "held_claims_count": record.held_claims_count or 0,
                "conversion_issue_count": issue_count,
                "uploaded_at": record.uploaded_at.isoformat() if record.uploaded_at else None,
                "processing_completed_at": record.processing_completed_at.isoformat() if record.processing_completed_at else None,
            }
        )

    response = JsonResponse({"success": True, "files": files})
    response["Cache-Control"] = "private, no-store, max-age=0"
    return response
