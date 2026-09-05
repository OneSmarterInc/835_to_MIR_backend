"""837 file-list API with accurate inbound source for SFTP-relayed files."""

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse

from project835.decorators import authenticated_api_required, json_api_errors

from .edi837_views import _client_for_request
from .models import EDI837File


def _is_sftp_inbound(item):
    """Identify records that actually travelled through the SFTP relay.

    Older records may have been indexed manually before the same bytes later
    arrived through 837_IN. Those rows can still have import_mode=MANUAL, but
    the relay replaces outbound_path with the verified remote 837_OUT path.
    New relay runs also persist import_mode=SFTP and remote_path.
    """
    if str(item.import_mode or "").upper() == "SFTP":
        return True
    if str(item.remote_path or "").strip():
        return True
    outbound = str(item.outbound_path or "").strip()
    return outbound.startswith("/")


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
        filters = Q(original_filename__icontains=query) | Q(stored_filename__icontains=query)
        if status_query in {choice[0] for choice in EDI837File.STATUS_CHOICES}:
            filters |= Q(status=status_query)
        if source_query in {choice[0] for choice in EDI837File.IMPORT_MODE_CHOICES}:
            filters |= Q(import_mode=source_query)
        if source_query == "SFTP":
            # Include historical relays that were indexed MANUAL before their
            # later SFTP arrival. A verified remote SFTP path begins with '/'.
            filters |= Q(remote_path__startswith="/") | Q(outbound_path__startswith="/")
        if "NOT PUSHED" in source_query or "NOT_PUSHED" in status_query:
            filters |= Q(outbound_path="")
        elif "OUTBOUND" in source_query or "PUSHED" in source_query:
            filters |= ~Q(outbound_path="")
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
        rows.append({
            "id": str(item.id),
            "file_name": item.original_filename,
            "status": item.status,
            "inbound_source": "SFTP" if sftp_inbound else item.get_import_mode_display(),
            "inbound_status": "Received",
            "outbound_status": "Pushed" if item.outbound_path else "Not pushed",
            "outbound_ready": bool(item.outbound_path),
            "claim_count": item.claim_count,
            "service_count": item.service_count,
            "total_charge_amount": str(item.total_charge_amount),
            "uploaded_at": item.uploaded_at.isoformat(),
            "processed_at": item.processed_at.isoformat() if item.processed_at else None,
        })

    # Repair historical duplicate rows lazily so every future API sees the
    # correct transport source too. This does not create duplicate file rows.
    if stale_sftp_ids:
        EDI837File.objects.filter(id__in=stale_sftp_ids).update(import_mode="SFTP")

    return JsonResponse({
        "success": True,
        "results": rows,
        "count": paginator.count,
        "page": page.number,
        "page_size": page_size,
        "pages": paginator.num_pages,
        "has_previous": page.has_previous(),
        "has_next": page.has_next(),
    })
