"""Attach database-backed file actions to paginated Universal Claim Search rows.

The core search stays in universal_claim_search.py. This wrapper adds stable file IDs
and the existing MPL download/view URLs so Universal Claim Search can reuse the
same source-file viewer and claim-slice download behavior as MPL.
"""

import json

from django.http import JsonResponse

from .edi837_views import _client_for_request
from .models import EDI835File, EDI837File, MIRFile, RECONFile
from .universal_claim_search import edi837_search as paginated_universal_claim_search


def _file_maps(client, rows):
    names = {"835": set(), "mir": set(), "recon": set(), "837": set()}
    for row in rows:
        lifecycle = row.get("lifecycle") or {}
        for file_type in names:
            source = lifecycle.get(file_type) or {}
            filename = str(source.get("file_name") or "").strip()
            if source.get("exists") and filename:
                names[file_type].add(filename)

    maps = {"835": {}, "mir": {}, "recon": {}, "837": {}}

    for record in (
        EDI835File.objects.filter(client=client, original_filename__in=names["835"])
        .only("id", "original_filename", "uploaded_at")
        .order_by("-uploaded_at")
    ):
        maps["835"].setdefault(record.original_filename, str(record.id))

    for record in (
        MIRFile.objects.filter(client=client, mir_filename__in=names["mir"])
        .only("id", "mir_filename", "converted_at")
        .order_by("-converted_at")
    ):
        maps["mir"].setdefault(record.mir_filename, str(record.id))

    for record in (
        RECONFile.objects.filter(client=client, original_filename__in=names["recon"])
        .only("id", "original_filename", "uploaded_at")
        .order_by("-uploaded_at")
    ):
        maps["recon"].setdefault(record.original_filename, str(record.id))

    for record in (
        EDI837File.objects.filter(client=client, original_filename__in=names["837"])
        .only("id", "original_filename", "uploaded_at")
        .order_by("-uploaded_at")
    ):
        maps["837"].setdefault(record.original_filename, str(record.id))

    return maps


def edi837_search(request):
    response = paginated_universal_claim_search(request)
    if response.status_code != 200:
        return response

    try:
        data = json.loads(response.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return response

    rows = data.get("results") or []
    if not rows:
        return response

    client = _client_for_request(request, request.GET.get("client_id"))
    if client is None:
        return response

    maps = _file_maps(client, rows)
    for row in rows:
        lifecycle = row.get("lifecycle") or {}
        for file_type in ("835", "mir", "recon", "837"):
            source = lifecycle.get(file_type) or {}
            if not source.get("exists"):
                continue
            filename = str(source.get("file_name") or "").strip()
            file_id = maps[file_type].get(filename)
            if not file_id:
                continue
            source["file_id"] = file_id
            source["download_url"] = f"/edi835/api/mpl-files/{file_type}/{file_id}/download/"
            source["claim_slice_url"] = f"/edi835/api/mpl-files/{file_type}/{file_id}/claim-slice/"
            lifecycle[file_type] = source
        row["lifecycle"] = lifecycle

    data["results"] = rows
    return JsonResponse(data, status=response.status_code)
