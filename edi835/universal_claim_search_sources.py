"""Attach existing MPL source-file actions to Universal Claim Search rows."""

import json

from django.http import JsonResponse

from .universal_claim_occurrence_search import edi837_search as occurrence_search


def edi837_search(request):
    response = occurrence_search(request)
    if response.status_code != 200:
        return response

    try:
        data = json.loads(response.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return response

    rows = data.get("results") or []
    for row in rows:
        lifecycle = row.get("lifecycle") or {}
        for file_type in ("837", "835", "mir", "recon"):
            source = lifecycle.get(file_type) or {}
            if not source.get("exists"):
                continue
            file_id = str(source.get("file_id") or "").strip()
            if not file_id:
                continue
            source["download_url"] = f"/edi835/api/mpl-files/{file_type}/{file_id}/download/"
            source["claim_slice_url"] = f"/edi835/api/mpl-files/{file_type}/{file_id}/claim-slice/"
            lifecycle[file_type] = source
        row["lifecycle"] = lifecycle

    data["results"] = rows
    return JsonResponse(data, status=response.status_code)
