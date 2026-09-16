"""Backend-paginated Universal Claim Search across 835, MIR, RECON, and 837."""

from __future__ import annotations

import json
import re

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse

from project835.decorators import authenticated_api_required, json_api_errors

from .claim_numbers import split_claim_number
from .edi837_views import _claim_row, _client_for_request
from .models import EDI835Claim, EDI837Claim, MIRClaim, RECONClaim

MIN_PARTIAL_DIGITS = 3
MAX_PARTIAL_DIGITS = 11
ALLOWED_FIELDS = {"all", "highmark", "internal", "patient", "835", "mir", "recon", "837"}


def _numbers(value):
    parts = split_claim_number(value)
    return (
        str(parts.get("highmark_claim_number") or "").strip(),
        str(parts.get("internal_claim_number") or "").strip(),
    )


def _page_args(request):
    try:
        page = max(1, int(request.GET.get("page", "1")))
        page_size = min(100, max(10, int(request.GET.get("page_size", "25"))))
    except (TypeError, ValueError):
        page, page_size = 1, 25
    return page, page_size


def _source_filters(query, field, partial_numeric=False):
    if not query:
        return None, None, None, None

    if partial_numeric:
        f835 = {
            "all": Q(highmark_claim_number__icontains=query) | Q(internal_claim_number__icontains=query),
            "highmark": Q(highmark_claim_number__icontains=query),
            "internal": Q(internal_claim_number__icontains=query),
        }.get(field)
        fmir = {
            "all": Q(claim_control_number__icontains=query),
            "highmark": Q(claim_control_number__icontains=query),
            "internal": Q(claim_control_number__icontains=query),
        }.get(field)
        frecon = {
            "all": Q(claim_control_number__icontains=query) | Q(patient_control_number__icontains=query),
            "highmark": Q(claim_control_number__icontains=query),
            "internal": Q(claim_control_number__icontains=query),
        }.get(field)
        f837 = {
            "all": (
                Q(claim_control_number__icontains=query)
                | Q(highmark_claim_number__icontains=query)
                | Q(internal_claim_number__icontains=query)
                | Q(patient_control_number__icontains=query)
                | Q(reference_9c__icontains=query)
            ),
            "highmark": Q(highmark_claim_number__icontains=query) | Q(claim_control_number__icontains=query),
            "internal": Q(internal_claim_number__icontains=query) | Q(reference_9c__icontains=query),
        }.get(field)
        return f835, fmir, frecon, f837

    f835 = {
        "all": (
            Q(highmark_claim_number__icontains=query)
            | Q(internal_claim_number__icontains=query)
            | Q(raw_claim__icontains=query)
            | Q(edi_file__original_filename__icontains=query)
            | Q(edi_file__stored_filename__icontains=query)
        ),
        "highmark": Q(highmark_claim_number__icontains=query),
        "internal": Q(internal_claim_number__icontains=query),
        "patient": Q(raw_claim__icontains=query),
        "835": Q(edi_file__original_filename__icontains=query) | Q(edi_file__stored_filename__icontains=query),
    }.get(field)
    fmir = {
        "all": (
            Q(claim_control_number__icontains=query)
            | Q(member_id__icontains=query)
            | Q(patient_first_name__icontains=query)
            | Q(patient_last_name__icontains=query)
            | Q(header_raw__icontains=query)
            | Q(mir_file__mir_filename__icontains=query)
        ),
        "highmark": Q(claim_control_number__icontains=query),
        "internal": Q(claim_control_number__icontains=query),
        "patient": Q(patient_first_name__icontains=query) | Q(patient_last_name__icontains=query),
        "mir": Q(mir_file__mir_filename__icontains=query),
    }.get(field)
    frecon = {
        "all": (
            Q(claim_control_number__icontains=query)
            | Q(patient_control_number__icontains=query)
            | Q(member_id__icontains=query)
            | Q(raw_record__icontains=query)
            | Q(recon_file__original_filename__icontains=query)
        ),
        "highmark": Q(claim_control_number__icontains=query),
        "internal": Q(claim_control_number__icontains=query),
        "patient": Q(raw_record__icontains=query),
        "recon": Q(recon_file__original_filename__icontains=query),
    }.get(field)
    f837 = {
        "all": (
            Q(claim_control_number__icontains=query)
            | Q(highmark_claim_number__icontains=query)
            | Q(internal_claim_number__icontains=query)
            | Q(patient_control_number__icontains=query)
            | Q(reference_9c__icontains=query)
            | Q(member_id__icontains=query)
            | Q(patient_first_name__icontains=query)
            | Q(patient_last_name__icontains=query)
            | Q(edi_file__original_filename__icontains=query)
        ),
        "highmark": Q(highmark_claim_number__icontains=query) | Q(claim_control_number__icontains=query),
        "internal": Q(internal_claim_number__icontains=query) | Q(reference_9c__icontains=query),
        "patient": Q(patient_first_name__icontains=query) | Q(patient_last_name__icontains=query),
        "837": Q(edi_file__original_filename__icontains=query),
    }.get(field)

    if field in {"all", "highmark"} and re.fullmatch(r"\d{12,25}", query):
        f835 = Q(highmark_claim_number=query)
        fmir = Q(claim_control_number__startswith=query)
        frecon = Q(claim_control_number__startswith=query) | Q(patient_control_number__startswith=query)
        f837 = Q(highmark_claim_number=query) | Q(claim_control_number=query) | Q(claim_control_number__startswith=query)

    return f835, fmir, frecon, f837


def _candidate_keys(client, query, field, partial_numeric=False):
    filter_835, filter_mir, filter_recon, filter_837 = _source_filters(query, field, partial_numeric)
    resolved = {}
    unresolved = set()

    def add(highmark, internal):
        highmark = str(highmark or "").strip()
        internal = str(internal or "").strip()
        if not highmark:
            return
        if internal:
            resolved.setdefault((highmark, internal.upper()), internal)
        else:
            unresolved.add(highmark)

    qs_835 = EDI835Claim.objects.filter(edi_file__client=client)
    qs_mir = MIRClaim.objects.filter(mir_file__client=client)
    qs_recon = RECONClaim.objects.filter(client=client, recon_file__file_kind="RECON")
    qs_837 = EDI837Claim.objects.filter(client=client)

    if query:
        qs_835 = qs_835.filter(filter_835) if filter_835 is not None else qs_835.none()
        qs_mir = qs_mir.filter(filter_mir) if filter_mir is not None else qs_mir.none()
        qs_recon = qs_recon.filter(filter_recon) if filter_recon is not None else qs_recon.none()
        qs_837 = qs_837.filter(filter_837) if filter_837 is not None else qs_837.none()

    for highmark, internal in qs_835.values_list("highmark_claim_number", "internal_claim_number").iterator(chunk_size=2000):
        add(highmark, internal)
    for claim_control in qs_mir.values_list("claim_control_number", flat=True).iterator(chunk_size=2000):
        add(*_numbers(claim_control))
    for claim_control, patient_control in qs_recon.values_list("claim_control_number", "patient_control_number").iterator(chunk_size=2000):
        add(*_numbers(claim_control or patient_control))
    for claim_control, highmark, internal, reference_9c in qs_837.values_list(
        "claim_control_number", "highmark_claim_number", "internal_claim_number", "reference_9c"
    ).iterator(chunk_size=2000):
        parsed_highmark, parsed_internal = _numbers(claim_control)
        add(highmark or parsed_highmark, internal or reference_9c or parsed_internal)

    for highmark in unresolved:
        matches = [key for key in resolved if key[0] == highmark]
        if len(matches) != 1:
            resolved.setdefault((highmark, ""), "")

    return sorted(
        ((highmark, internal_key, resolved[(highmark, internal_key)]) for highmark, internal_key in resolved),
        key=lambda item: (item[0], item[1]),
    )


def _startswith_filter(field, values):
    lookup = Q()
    for value in values:
        lookup |= Q(**{f"{field}__istartswith": value})
    return lookup


def _event(source_type, item, highmark, internal):
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
    findings = []
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
            return " ".join(filter(None, (fields[4].strip(), fields[3].strip())))
    return ""


def _page_rows(client, page_keys):
    groups = {
        (highmark, internal_key): {"highmark": highmark, "internal": internal, "history": []}
        for highmark, internal_key, internal in page_keys
    }
    by_highmark = {}
    for key in groups:
        by_highmark.setdefault(key[0], []).append(key)
    highmarks = list(by_highmark)
    if not highmarks:
        return []

    cross_filter = _startswith_filter("claim_control_number", highmarks)
    source_835 = EDI835Claim.objects.select_related("edi_file").filter(
        edi_file__client=client, highmark_claim_number__in=highmarks
    ).defer("raw_claim").order_by("-edi_file__uploaded_at", "claim_sequence")
    source_mir = MIRClaim.objects.select_related("mir_file").filter(
        cross_filter, mir_file__client=client
    ).defer("header_raw").order_by("-mir_file__converted_at")
    source_recon = RECONClaim.objects.select_related("recon_file").filter(
        cross_filter, client=client, recon_file__file_kind="RECON"
    ).defer("raw_record").order_by("-recon_file__uploaded_at")
    source_837 = EDI837Claim.objects.select_related("edi_file").filter(client=client).filter(
        Q(highmark_claim_number__in=highmarks) | cross_filter
    ).defer("raw_claim").order_by("-edi_file__processed_at", "-edi_file__uploaded_at", "-id")

    def target_key(highmark, internal):
        highmark = str(highmark or "").strip()
        internal = str(internal or "").strip()
        exact = (highmark, internal.upper())
        if exact in groups:
            return exact
        if not internal:
            keys = by_highmark.get(highmark, [])
            if len(keys) == 1:
                return keys[0]
            blank = (highmark, "")
            if blank in groups:
                return blank
        return None

    def add_source(highmark, internal, source_type, item):
        key = target_key(highmark, internal)
        if key is None:
            return
        group = groups[key]
        group.setdefault(source_type, item)
        group["history"].append(_event(source_type, item, group["highmark"], group["internal"]))

    for item in source_835:
        add_source(item.highmark_claim_number, item.internal_claim_number, "835", item)
    for item in source_mir:
        add_source(*_numbers(item.claim_control_number), "mir", item)
    for item in source_recon:
        add_source(*_numbers(item.claim_control_number or item.patient_control_number), "recon", item)
    for item in source_837:
        parsed_highmark, parsed_internal = _numbers(item.claim_control_number)
        add_source(
            item.highmark_claim_number or parsed_highmark,
            item.internal_claim_number or item.reference_9c or parsed_internal,
            "837",
            item,
        )

    rows = []
    for highmark, internal_key, _internal in page_keys:
        sources = groups[(highmark, internal_key)]
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

        history = sorted(sources["history"], key=lambda event: event.get("arrived_at") or "")
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
    return rows


@authenticated_api_required
@json_api_errors
def edi837_search(request):
    """Browse and search the entire client claim population with backend pagination."""
    if request.method != "GET":
        return JsonResponse({"success": False, "error": "Only GET is allowed."}, status=405)

    client = _client_for_request(request, request.GET.get("client_id"))
    if client is None:
        return JsonResponse({"success": False, "error": "Select an authorized client."}, status=400)

    query = str(request.GET.get("q") or "").strip()
    field = str(request.GET.get("field") or "all").strip().lower()
    if field not in ALLOWED_FIELDS:
        return JsonResponse({"success": False, "error": "Invalid search column."}, status=400)

    page_number, page_size = _page_args(request)
    if query.isdigit() and len(query) < MIN_PARTIAL_DIGITS:
        return JsonResponse({
            "success": True,
            "query": query,
            "count": 0,
            "results": [],
            "page": 1,
            "page_size": page_size,
            "pages": 0,
            "has_previous": False,
            "has_next": False,
            "search_mode": "too_short",
            "minimum_characters": MIN_PARTIAL_DIGITS,
        })

    partial_numeric = bool(
        field in {"all", "highmark", "internal"}
        and re.fullmatch(rf"\d{{{MIN_PARTIAL_DIGITS},{MAX_PARTIAL_DIGITS}}}", query)
    )
    keys = _candidate_keys(client, query, field, partial_numeric)
    total = len(keys)
    paginator = Paginator(keys, page_size)
    page_obj = paginator.get_page(page_number)
    page_keys = list(page_obj.object_list)
    rows = _page_rows(client, page_keys)

    return JsonResponse({
        "success": True,
        "query": query,
        "field": field,
        "count": total,
        "results": rows,
        "page": page_obj.number,
        "page_size": page_size,
        "pages": paginator.num_pages if total else 0,
        "has_previous": page_obj.has_previous(),
        "has_next": page_obj.has_next(),
        "search_mode": "partial_numeric_fast" if partial_numeric else ("browse" if not query else "backend_paginated"),
    })
