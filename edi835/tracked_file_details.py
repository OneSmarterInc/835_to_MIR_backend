"""Lazy tracked-file detail endpoints for heavyweight conversion data."""

from django.http import JsonResponse

from admin_panel.access_control import scope_client_queryset

from .models import EDI835File, MIRFile


def _scoped_files(request):
    qs = scope_client_queryset(
        EDI835File.objects.select_related("client", "mir_file"),
        request.user,
    )
    requested_client_id = str(request.GET.get("client_id") or "").strip()
    if requested_client_id:
        qs = qs.filter(client_id=requested_client_id)
    return qs


def _is_blocking(finding):
    severity = str((finding or {}).get("severity") or "").upper()
    return severity in {"HOLD", "REFUSE"}


def _claim_key(finding, index):
    finding = finding or {}
    claim_number = finding.get("claim_number") or finding.get("claim_control_number")
    claim_number = str(claim_number or f"held-claim-{index + 1}")
    claim_index = str(finding.get("claim_index") or "").strip()
    return f"{claim_number}:claim-index:{claim_index}" if claim_index else claim_number


def _conversion_findings_with_previous_835(record):
    """Enrich duplicate findings with the 835 source that created the prior MIR.

    Historical conversion findings already persist the previous MIR id/name.  Resolve
    that MIR back through MIRFile.source_835 so the Checks screen can show exactly
    which 835 input(s) produced the previous MIR.  This is response-only enrichment;
    stored audit findings are never rewritten.
    """
    raw_findings = list(record.conversion_findings or [])
    if not raw_findings:
        return []

    previous_ids = {
        str(finding.get("previous_mir_id") or "").strip()
        for finding in raw_findings
        if str(finding.get("previous_mir_id") or "").strip()
    }
    previous_names = {
        str(finding.get("previous_mir_filename") or "").strip()
        for finding in raw_findings
        if str(finding.get("previous_mir_filename") or "").strip()
    }

    candidates = MIRFile.objects.select_related("source_835").filter(client_id=record.client_id)
    by_id = {
        str(mir.id): mir
        for mir in candidates.filter(id__in=previous_ids)
    } if previous_ids else {}
    by_name = {
        mir.mir_filename: mir
        for mir in candidates.filter(mir_filename__in=previous_names)
    } if previous_names else {}

    enriched = []
    for original in raw_findings:
        finding = dict(original or {})
        previous_id = str(finding.get("previous_mir_id") or "").strip()
        previous_name = str(finding.get("previous_mir_filename") or "").strip()
        previous_mir = by_id.get(previous_id) or by_name.get(previous_name)
        if previous_mir is not None and previous_mir.source_835 is not None:
            source_835 = str(previous_mir.source_835.original_filename or "").strip()
            if source_835:
                finding["previous_835_filename"] = source_835
                # Existing frontend already renders previous_mir_filename in the
                # Previous MIR File column. Include the source there as well so
                # historical rows gain the information without a data migration.
                finding["previous_mir_filename"] = (
                    f"{previous_name or previous_mir.mir_filename} — 835: {source_835}"
                )
        enriched.append(finding)
    return enriched


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
                "conversion_findings": _conversion_findings_with_previous_835(record),
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
