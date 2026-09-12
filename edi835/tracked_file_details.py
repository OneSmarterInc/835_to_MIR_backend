"""Lazy tracked-file detail endpoint for large conversion findings payloads."""

from django.http import JsonResponse

from admin_panel.access_control import scope_client_queryset

from .models import EDI835File


def tracked_file_details(request, file_id):
    """Return heavyweight details for one visible tracked file only when requested."""
    qs = scope_client_queryset(
        EDI835File.objects.select_related("client", "mir_file"),
        request.user,
    )

    record = qs.filter(id=file_id).first()
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
