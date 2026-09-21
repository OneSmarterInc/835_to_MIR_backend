"""Fast path for short numeric Universal Claim Search queries.

The legacy universal search intentionally supports very broad text matching. For
short numeric fragments that broad path becomes pathological because it scans
large raw EDI payload columns and then performs expensive cross-source expansion.
This module keeps 3-11 digit claim fragments on normalized identity columns only,
which is the behavior users expect when entering part of a claim number.
"""

from __future__ import annotations

import json
import re

from django.db.models import Q
from django.http import JsonResponse

from project835.decorators import authenticated_api_required, json_api_errors

from .claim_numbers import split_claim_number
from .edi837_views import (
    _claim_row,
    _client_for_request,
    edi837_search as legacy_edi837_search,
)
from .models import EDI835Claim, EDI837Claim, MIRClaim, RECONClaim


MIN_PARTIAL_DIGITS = 3
MAX_PARTIAL_DIGITS = 11
SOURCE_SCAN_LIMIT = 150


def _numbers(value):
    parts = split_claim_number(value)
    return (
        str(parts.get("highmark_claim_number") or "").strip(),
        str(parts.get("internal_claim_number") or "").strip(),
    )


def _patient_from_835(item):
    if not item:
        return ""
    data = item.segment_data or {}
    direct = str(data.get("patient_name") or "").strip()
    if direct:
        return direct
    first = str(data.get("patient_first_name") or data.get("first_name") or "").strip()
    last = str(data.get("patient_last_name") or data.get("last_name") or "").strip()
    if first or last:
        return " ".join(filter(None, (first, last)))
    for segment in data.get("segments", []):
        fields = str(segment).split("*")
        if len(fields) > 4 and fields[0].upper() == "NM1" and fields[1].upper() == "QC":
            return " ".join(filter(None, (
                fields[4].strip() if len(fields) > 4 else "",
                fields[3].strip() if len(fields) > 3 else "",
            )))
    return ""


def _event(source_type, item, highmark, internal):
    findings = []
    if source_type == "835":
        file_obj = item.edi_file
        file_name = file_obj.original_filename
        arrived_at = file_obj.uploaded_at
        status = file_obj.status
        candidates = file_obj.conversion_findings or []
    elif source_type == "mir":
        file_obj = item.mir_file
        file_name = file_obj.mir_filename
        arrived_at = file_obj.converted_at
        status = file_obj.status
        candidates = []
    elif source_type == "recon":
        file_obj = item.recon_file
        file_name = file_obj.original_filename
        arrived_at = file_obj.uploaded_at
        status = file_obj.status
        candidates = file_obj.parsing_findings or []
    else:
        file_obj = item.edi_file
        file_name = file_obj.original_filename
        arrived_at = file_obj.processed_at or file_obj.uploaded_at
        status = file_obj.status
        candidates = []

    needles = [str(value).upper() for value in (highmark, internal) if str(value or "").strip()]
    for finding in candidates:
        searchable = json.dumps(finding, default=str).upper()
        if any(needle in searchable for needle in needles):
            findings.append(finding)

    return {
        "source": source_type,
        "file_name": file_name,
        "arrived_at": arrived_at.isoformat() if arrived_at else None,
        "status": status,
        "internal_claim_number": internal,
        "findings": findings,
    }


def _partial_numeric_search(request, client, query, limit):
    # IMPORTANT: do not add raw_claim/header_raw/raw_record here. Those large
    # text fields are precisely what caused short fragments to hit the nginx
    # upstream timeout. Search only normalized identity columns.
    source_835 = list(
        EDI835Claim.objects.select_related("edi_file")
        .filter(edi_file__client=client)
        .filter(
            Q(highmark_claim_number__icontains=query)
            | Q(internal_claim_number__icontains=query)
        )[:SOURCE_SCAN_LIMIT]
    )
    source_mir = list(
        MIRClaim.objects.select_related("mir_file")
        .filter(mir_file__client=client, claim_control_number__icontains=query)[:SOURCE_SCAN_LIMIT]
    )
    source_recon = list(
        RECONClaim.objects.select_related("recon_file")
        .filter(client=client, recon_file__file_kind="RECON")
        .filter(
            Q(claim_control_number__icontains=query)
            | Q(patient_control_number__icontains=query)
        )[:SOURCE_SCAN_LIMIT]
    )
    source_837 = list(
        EDI837Claim.objects.select_related("edi_file")
        .filter(client=client)
        .filter(
            Q(claim_control_number__icontains=query)
            | Q(highmark_claim_number__icontains=query)
            | Q(internal_claim_number__icontains=query)
            | Q(patient_control_number__icontains=query)
            | Q(reference_9c__icontains=query)
        )[:SOURCE_SCAN_LIMIT]
    )

    grouped = {}
    unresolved = {}

    def add(highmark, internal, source_type, item):
        highmark = str(highmark or "").strip()
        internal = str(internal or "").strip()
        if not highmark:
            return
        if internal:
            key = (highmark, internal.upper())
            group = grouped.setdefault(key, {"highmark": highmark, "internal": internal, "history": []})
        else:
            group = unresolved.setdefault(highmark, {"highmark": highmark, "internal": "", "history": []})
        group.setdefault(source_type, item)
        group["history"].append(_event(source_type, item, highmark, internal))

    for item in source_835:
        add(item.highmark_claim_number, item.internal_claim_number, "835", item)
    for item in source_mir:
        highmark, internal = _numbers(item.claim_control_number)
        add(highmark, internal, "mir", item)
    for item in source_recon:
        highmark, internal = _numbers(item.claim_control_number)
        add(highmark, internal, "recon", item)
    for item in source_837:
        parsed_highmark, parsed_internal = _numbers(item.claim_control_number)
        add(
            item.highmark_claim_number or parsed_highmark,
            item.internal_claim_number or item.reference_9c or parsed_internal,
            "837",
            item,
        )

    for highmark, blank in unresolved.items():
        keys = [key for key in grouped if key[0] == highmark]
        if len(keys) == 1:
            target = grouped[keys[0]]
        else:
            target = grouped.setdefault((highmark, ""), {"highmark": highmark, "internal": "", "history": []})
        for source_type in ("835", "mir", "recon", "837"):
            if source_type in blank:
                target.setdefault(source_type, blank[source_type])
        target["history"].extend(blank["history"])

    rows = []
    for sources in sorted(grouped.values(), key=lambda row: (row["highmark"], row["internal"].upper()))[:limit]:
        highmark = sources["highmark"]
        internal = sources["internal"]
        item_835 = sources.get("835")
        mir = sources.get("mir")
        recon = sources.get("recon")
        claim_837 = sources.get("837")

        mir_internal = _numbers(mir.claim_control_number)[1] if mir else ""
        recon_internal = _numbers(recon.claim_control_number)[1] if recon else ""

        patient_name = ""
        if claim_837:
            patient_name = " ".join(filter(None, (claim_837.patient_first_name, claim_837.patient_last_name))).strip()
        if not patient_name and mir:
            patient_name = " ".join(filter(None, (mir.patient_first_name, mir.patient_last_name))).strip()
        if not patient_name:
            patient_name = _patient_from_835(item_835)

        lifecycle = {
            "835": {
                "exists": bool(item_835),
                "file_name": item_835.edi_file.original_filename if item_835 else "",
                "arrived_at": item_835.edi_file.uploaded_at.isoformat() if item_835 else None,
                "status": item_835.edi_file.status if item_835 else "",
                "internal_claim_number": item_835.internal_claim_number if item_835 else "",
            },
            "mir": {
                "exists": bool(mir),
                "file_name": mir.mir_file.mir_filename if mir else "",
                "arrived_at": mir.mir_file.converted_at.isoformat() if mir else None,
                "internal_claim_number": mir_internal,
            },
            "recon": {
                "exists": bool(recon),
                "file_name": recon.recon_file.original_filename if recon else "",
                "arrived_at": recon.recon_file.uploaded_at.isoformat() if recon else None,
                "internal_claim_number": recon_internal,
            },
            "837": {
                "exists": bool(claim_837),
                "file_name": claim_837.edi_file.original_filename if claim_837 else "",
                "arrived_at": (
                    (claim_837.edi_file.processed_at or claim_837.edi_file.uploaded_at).isoformat()
                    if claim_837 else None
                ),
                "status": claim_837.edi_file.status if claim_837 else "",
                "internal_claim_number": (
                    claim_837.internal_claim_number or claim_837.reference_9c if claim_837 else ""
                ),
            },
        }

        history = sorted(sources.get("history", []), key=lambda event: event.get("arrived_at") or "")
        findings = []
        seen = set()
        for event in history:
            for finding in event.get("findings", []):
                signature = json.dumps(finding, sort_keys=True, default=str)
                if signature not in seen:
                    findings.append(finding)
                    seen.add(signature)
        codes = {
            str(finding.get("rule_code") or finding.get("code") or finding.get("error_code") or "").upper()
            for finding in findings if isinstance(finding, dict)
        }
        duplicate = bool(codes & {"DUPLICATE_ICN", "DUPLICATE_RECENT_MIR", "DUPLICATE_CLAIM_NUMBER"})
        held = any(
            str(finding.get("decision") or finding.get("severity") or "").upper()
            in {"HOLD", "HELD", "REFUSE", "ERROR"}
            for finding in findings if isinstance(finding, dict)
        )
        operational = {
            "duplicate": duplicate,
            "held": held,
            "status": "HELD" if held else ("DUPLICATE" if duplicate else "CLEAR"),
            "findings": findings,
            "history": history,
            "occurrence_count": len(history),
        }

        if claim_837:
            row = _claim_row(claim_837)
            row.update({
                "has_837": True,
                "highmark_claim_number": highmark,
                "internal_claim_number": internal,
                "patient_name": row.get("patient_name") or patient_name,
                "lifecycle": lifecycle,
                "operational": operational,
            })
        else:
            member_id = (mir.member_id if mir else "") or (recon.member_id if recon else "")
            row = {
                "id": f"universal:{highmark}:{internal}",
                "claim_number": highmark,
                "highmark_claim_number": highmark,
                "internal_claim_number": internal,
                "patient_name": patient_name,
                "member_id": member_id,
                "total_charge_amount": str(item_835.total_charge_amount if item_835 else 0),
                "service_count": item_835.service_count if item_835 else 0,
                "file_name": "",
                "processed_at": None,
                "has_837": False,
                "lifecycle": lifecycle,
                "operational": operational,
            }
        rows.append(row)

    return JsonResponse({
        "success": True,
        "query": query,
        "count": len(rows),
        "results": rows,
        "search_mode": "partial_numeric_fast",
    })


@authenticated_api_required
@json_api_errors
def edi837_search(request):
    """Use a bounded fast path for short numeric fragments; preserve legacy search otherwise."""
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)

    query = str(request.GET.get("q") or "").strip()
    field = str(request.GET.get("field") or "all").strip().lower()

    # The UI sends requests while the user is typing. One- and two-character
    # searches are too broad to be useful and historically triggered full-table
    # scans before the user had finished entering the claim fragment.
    if query.isdigit() and len(query) < MIN_PARTIAL_DIGITS:
        return JsonResponse({
            "success": True,
            "query": query,
            "count": 0,
            "results": [],
            "search_mode": "too_short",
            "minimum_characters": MIN_PARTIAL_DIGITS,
        })

    is_partial_numeric = bool(
        field in {"all", "highmark", "internal"}
        and re.fullmatch(rf"\d{{{MIN_PARTIAL_DIGITS},{MAX_PARTIAL_DIGITS}}}", query)
    )
    if not is_partial_numeric:
        return legacy_edi837_search(request)

    client = _client_for_request(request, request.GET.get("client_id"))
    if client is None:
        return JsonResponse({"success": False, "error": "Select an authorized client."}, status=400)
    try:
        limit = min(100, max(1, int(request.GET.get("limit", "50"))))
    except ValueError:
        limit = 50

    return _partial_numeric_search(request, client, query, limit)
