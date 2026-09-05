"""837 file-list API with accurate inbound source and saved naming format."""

import os

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse

from project835.decorators import authenticated_api_required, json_api_errors

from .edi837_views import _client_for_request
from .edi837_naming_views import get_saved_837_filename_format
from .models import EDI837File


def _is_sftp_inbound(item):
    if str(item.import_mode or "").upper() == "SFTP":
        return True
    return bool(str(item.remote_path or "").strip())


def _confirmed_remote_outbound(item):
    """Only an absolute remote SFTP path counts as a successful push.

    ingest_837 also creates a local 837_out working copy and stores that local
    relative path in outbound_path.  That local copy must never be displayed as
    a successful SFTP delivery.
    """
    outbound = str(item.outbound_path or "").strip()
    return outbound if outbound.startswith("/") else ""


def _inbound_filename(item, sftp_inbound):
    remote = str(item.remote_path or "").strip()
    if sftp_inbound and remote:
        name = os.path.basename(remote.rstrip("/"))
        if name:
            return name
    return item.original_filename


@authenticated_api_required
@json_api_errors
def edi837_files(request):
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)

    client = _client_for_request(request, request.GET.get("client_id"))
    if client is None:
        return JsonResponse({"success": False, "error": "Select an authorized client."}, status=400)

    query = str(request.GET.get("q") or "").strip()
    files = EDI837File.objects.filter(client=client)

    if query:
        status_query = query.upper().replace(" ", "_")
        source_query = query.upper()
        filters = (
            Q(original_filename__icontains=query)
            | Q(stored_filename__icontains=query)
            | Q(remote_path__icontains=query)
            | Q(outbound_path__icontains=query)
        )
        if status_query in {choice[0] for choice in EDI837File.STATUS_CHOICES}:
            filters |= Q(status=status_query)
        if source_query in {choice[0] for choice in EDI837File.IMPORT_MODE_CHOICES}:
            filters |= Q(import_mode=source_query)
        if source_query == "SFTP":
            filters |= Q(remote_path__startswith="/")
        files = files.filter(filters)

    try:
        page_number = max(1, int(request.GET.get("page", "1")))
        page_size = min(100, max(10, int(request.GET.get("page_size", "20"))))
    except ValueError:
        page_number, page_size = 1, 20

    paginator = Paginator(files.order_by("-uploaded_at"), page_size)
    page = paginator.get_page(page_number)

    rows = []
    stale_sftp_ids = []
    for item in page.object_list:
        sftp_inbound = _is_sftp_inbound(item)
        if sftp_inbound and item.import_mode != "SFTP":
            stale_sftp_ids.append(item.id)

        remote_outbound = _confirmed_remote_outbound(item)
        inbound_name = _inbound_filename(item, sftp_inbound)
        outbound_name = os.path.basename(remote_outbound.rstrip("/")) if remote_outbound else ""

        rows.append({
            "id": str(item.id),
            "file_name": inbound_name,
            "original_file_name": inbound_name,
            "stored_file_name": item.stored_filename,
            "outbound_file_name": outbound_name,
            "inbound_path": str(item.remote_path or ""),
            "outbound_path": remote_outbound,
            "status": item.status,
            "inbound_source": "SFTP" if sftp_inbound else item.get_import_mode_display(),
            "inbound_status": "Received",
            "outbound_status": "Pushed" if remote_outbound else "Not pushed",
            "outbound_ready": bool(remote_outbound),
            "claim_count": item.claim_count,
            "service_count": item.service_count,
            "total_charge_amount": str(item.total_charge_amount),
            "uploaded_at": item.uploaded_at.isoformat(),
            "processed_at": item.processed_at.isoformat() if item.processed_at else None,
        })

    if stale_sftp_ids:
        EDI837File.objects.filter(id__in=stale_sftp_ids).update(import_mode="SFTP")

    return JsonResponse({
        "success": True,
        "filename_format": get_saved_837_filename_format(client),
        "results": rows,
        "count": paginator.count,
        "page": page.number,
        "page_size": page_size,
        "pages": paginator.num_pages,
        "has_previous": page.has_previous(),
        "has_next": page.has_next(),
    })
