import io
import os
import zipfile
from pathlib import PurePath

from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from admin_panel.access_control import can_access_client, scope_client_queryset
from edi835.models import EDI835File, EDI837File, MIRFile, RECONFile


def _scope(queryset, request, client_id):
    if client_id:
        if not can_access_client(request.user, client_id):
            return None
        return queryset.filter(client_id=client_id)
    return scope_client_queryset(queryset, request.user)


def _inventory(request, client_id):
    groups = {
        "835": _scope(EDI835File.objects.all(), request, client_id),
        "837": _scope(EDI837File.objects.all(), request, client_id),
        "MIR": _scope(MIRFile.objects.all(), request, client_id),
        "RECON": _scope(RECONFile.objects.all(), request, client_id),
    }
    if any(value is None for value in groups.values()):
        return None
    counts = {name: queryset.count() for name, queryset in groups.items()}
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "available_types": [name for name, count in counts.items() if count > 0],
    }


@csrf_exempt
def api_download_archive_zip(request):
    """Return a live client archive manifest or ZIP built from persisted records."""
    if request.method != "GET":
        return JsonResponse({"error": "Only GET is allowed."}, status=405)

    client_id = str(request.GET.get("client") or "").strip()
    download_type = str(request.GET.get("type") or "all").lower().strip()
    if download_type not in {"mir", "835", "837", "recon", "both", "all"}:
        return JsonResponse({"error": "Invalid archive type."}, status=400)

    inventory = _inventory(request, client_id)
    if inventory is None:
        return JsonResponse({
            "error": "Temporary approved client access is required.",
            "code": "CLIENT_GRANT_REQUIRED",
        }, status=403)

    if request.GET.get("summary") in {"1", "true", "yes"}:
        return JsonResponse({"success": True, **inventory})

    selected_types = {
        "835": download_type in {"835", "both", "all"},
        "837": download_type in {"837", "all"},
        "MIR": download_type in {"mir", "both", "all"},
        "RECON": download_type in {"recon", "all"},
    }

    memory = io.BytesIO()
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

    with zipfile.ZipFile(memory, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        if selected_types["835"]:
            records = _scope(EDI835File.objects.all(), request, client_id)
            for record in records.only("id", "original_filename", "stored_filename", "input_file_content").iterator():
                add_text(zf, "835", record.original_filename or record.stored_filename, record.input_file_content, record.id)

        if selected_types["837"]:
            records = _scope(EDI837File.objects.all(), request, client_id)
            for record in records.only("id", "original_filename", "stored_filename", "file_content").iterator():
                add_text(zf, "837", record.original_filename or record.stored_filename, record.file_content, record.id)

        if selected_types["MIR"]:
            records = _scope(MIRFile.objects.all(), request, client_id)
            for record in records.only("id", "mir_filename", "file_content").iterator():
                add_text(zf, "MIR", record.mir_filename, record.file_content, record.id)

        if selected_types["RECON"]:
            records = _scope(RECONFile.objects.all(), request, client_id)
            for record in records.only("id", "original_filename", "stored_filename", "file_content").iterator():
                add_text(zf, "RECON", record.original_filename or record.stored_filename, record.file_content, record.id)

    memory.seek(0)
    if not added_paths:
        return JsonResponse({"error": "No client files were found to archive."}, status=404)

    response = HttpResponse(memory.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = 'attachment; filename="client_data_archive.zip"'
    response["X-OneSmarter-Archive-File-Count"] = str(len(added_paths))
    return response
