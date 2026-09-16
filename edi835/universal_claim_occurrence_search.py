"""Occurrence-first, backend-paginated Universal Claim Search.

Row semantics intentionally follow the operational file flow:
837 occurrence -> matching 835 occurrence -> MIR -> RECON.

Important rules:
* Every stored 837 claim occurrence is its own row, even when several 837 rows
  share the same Highmark claim number.
* 837 never supplies the displayed internal claim number.
* Matching 835 occurrences are paired oldest-to-oldest with 837 occurrences
  having the same Highmark number. Extra 835 occurrences become their own rows.
* MIR and RECON attach only when both Highmark and the 835-derived internal
  claim number match. Multiple occurrences are paired in chronological order.
* Search filtering and pagination are performed on the server across the full
  client population.
"""

from __future__ import annotations

import re
from collections import defaultdict

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse

from project835.decorators import authenticated_api_required, json_api_errors

from .claim_numbers import split_claim_number
from .edi837_views import _client_for_request
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
        return (
            {
                "all": Q(highmark_claim_number__icontains=query) | Q(internal_claim_number__icontains=query),
                "highmark": Q(highmark_claim_number__icontains=query),
                "internal": Q(internal_claim_number__icontains=query),
            }.get(field),
            {
                "all": Q(claim_control_number__icontains=query),
                "highmark": Q(claim_control_number__icontains=query),
                "internal": Q(claim_control_number__icontains=query),
            }.get(field),
            {
                "all": Q(claim_control_number__icontains=query) | Q(patient_control_number__icontains=query),
                "highmark": Q(claim_control_number__icontains=query),
                "internal": Q(claim_control_number__icontains=query),
            }.get(field),
            {
                "all": Q(highmark_claim_number__icontains=query) | Q(claim_control_number__icontains=query),
                "highmark": Q(highmark_claim_number__icontains=query) | Q(claim_control_number__icontains=query),
                # 837 does not provide the MIR Relay internal claim number.
                "internal": None,
            }.get(field),
        )

    filter_835 = {
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
    filter_mir = {
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
    filter_recon = {
        "all": (
            Q(claim_control_number__icontains=query)
            | Q(patient_control_number__icontains=query)
            | Q(member_id__icontains=query)
            | Q(raw_record__icontains=query)
            | Q(recon_file__original_filename__icontains=query)
        ),
        "highmark": Q(claim_control_number__icontains=query) | Q(patient_control_number__icontains=query),
        "internal": Q(claim_control_number__icontains=query),
        "patient": Q(raw_record__icontains=query),
        "recon": Q(recon_file__original_filename__icontains=query),
    }.get(field)
    filter_837 = {
        "all": (
            Q(claim_control_number__icontains=query)
            | Q(highmark_claim_number__icontains=query)
            | Q(patient_control_number__icontains=query)
            | Q(member_id__icontains=query)
            | Q(patient_first_name__icontains=query)
            | Q(patient_last_name__icontains=query)
            | Q(edi_file__original_filename__icontains=query)
        ),
        "highmark": Q(highmark_claim_number__icontains=query) | Q(claim_control_number__icontains=query),
        "internal": None,
        "patient": Q(patient_first_name__icontains=query) | Q(patient_last_name__icontains=query),
        "837": Q(edi_file__original_filename__icontains=query),
    }.get(field)

    if field in {"all", "highmark"} and re.fullmatch(r"\d{12,25}", query):
        filter_835 = Q(highmark_claim_number=query)
        filter_mir = Q(claim_control_number__startswith=query)
        filter_recon = Q(claim_control_number__startswith=query) | Q(patient_control_number__startswith=query)
        filter_837 = Q(highmark_claim_number=query) | Q(claim_control_number=query) | Q(claim_control_number__startswith=query)

    return filter_835, filter_mir, filter_recon, filter_837


def _matched_source_ids(client, query, field, partial_numeric=False):
    """Return exact matching source claim IDs and the Highmark population to assemble."""
    if not query:
        return None, None

    f835, fmir, frecon, f837 = _source_filters(query, field, partial_numeric)
    matched = {"835": set(), "mir": set(), "recon": set(), "837": set()}
    highmarks = set()

    if f835 is not None:
        for claim_id, highmark in (
            EDI835Claim.objects.filter(edi_file__client=client).filter(f835)
            .values_list("id", "highmark_claim_number").iterator(chunk_size=2000)
        ):
            matched["835"].add(str(claim_id))
            if highmark:
                highmarks.add(str(highmark).strip())

    if fmir is not None:
        for claim_id, claim_control in (
            MIRClaim.objects.filter(mir_file__client=client).filter(fmir)
            .values_list("id", "claim_control_number").iterator(chunk_size=2000)
        ):
            matched["mir"].add(str(claim_id))
            highmark, _internal = _numbers(claim_control)
            if highmark:
                highmarks.add(highmark)

    if frecon is not None:
        for claim_id, claim_control, patient_control in (
            RECONClaim.objects.filter(client=client, recon_file__file_kind="RECON").filter(frecon)
            .values_list("id", "claim_control_number", "patient_control_number").iterator(chunk_size=2000)
        ):
            matched["recon"].add(str(claim_id))
            highmark, _internal = _numbers(claim_control or patient_control)
            if highmark:
                highmarks.add(highmark)

    if f837 is not None:
        for claim_id, claim_control, highmark_value in (
            EDI837Claim.objects.filter(client=client).filter(f837)
            .values_list("id", "claim_control_number", "highmark_claim_number").iterator(chunk_size=2000)
        ):
            matched["837"].add(str(claim_id))
            parsed_highmark, _internal = _numbers(claim_control)
            highmark = str(highmark_value or parsed_highmark or "").strip()
            if highmark:
                highmarks.add(highmark)

    return matched, highmarks


def _occurrence_time_key(item):
    value = item.get("arrived_at")
    return (value is None, value, item.get("claim_sequence", 0), str(item.get("claim_id") or ""))


def _highmark_scope(queryset, highmarks, direct_field=None, combined_field=None):
    if highmarks is None:
        return queryset
    if not highmarks:
        return queryset.none()
    if direct_field:
        direct = Q(**{f"{direct_field}__in": list(highmarks)})
        if combined_field and len(highmarks) <= 250:
            for highmark in highmarks:
                direct |= Q(**{f"{combined_field}__startswith": highmark})
        return queryset.filter(direct)
    if combined_field and len(highmarks) <= 250:
        lookup = Q()
        for highmark in highmarks:
            lookup |= Q(**{f"{combined_field}__startswith": highmark})
        return queryset.filter(lookup)
    return queryset


def _load_occurrences(client, highmarks=None):
    """Load lightweight normalized occurrence descriptors for the candidate Highmarks."""
    occurrences = defaultdict(lambda: {"837": [], "835": [], "mir": [], "recon": []})

    q837 = _highmark_scope(
        EDI837Claim.objects.filter(client=client), highmarks,
        direct_field="highmark_claim_number", combined_field="claim_control_number",
    )
    for values in q837.values_list(
        "id", "claim_sequence", "claim_control_number", "highmark_claim_number",
        "edi_file_id", "edi_file__original_filename", "edi_file__status",
        "edi_file__uploaded_at", "edi_file__processed_at",
    ).iterator(chunk_size=2000):
        (claim_id, sequence, claim_control, stored_highmark, file_id, filename,
         status, uploaded_at, processed_at) = values
        parsed_highmark, _parsed_internal = _numbers(claim_control)
        highmark = str(stored_highmark or parsed_highmark or "").strip()
        if not highmark or (highmarks is not None and highmark not in highmarks):
            continue
        occurrences[highmark]["837"].append({
            "claim_id": str(claim_id), "claim_sequence": sequence,
            "highmark": highmark, "internal": "", "file_id": str(file_id),
            "filename": filename, "status": status,
            "arrived_at": processed_at or uploaded_at,
        })

    q835 = _highmark_scope(
        EDI835Claim.objects.filter(edi_file__client=client), highmarks,
        direct_field="highmark_claim_number",
    )
    for values in q835.values_list(
        "id", "claim_sequence", "highmark_claim_number", "internal_claim_number",
        "edi_file_id", "edi_file__original_filename", "edi_file__status", "edi_file__uploaded_at",
    ).iterator(chunk_size=2000):
        claim_id, sequence, highmark, internal, file_id, filename, status, uploaded_at = values
        highmark = str(highmark or "").strip()
        if not highmark or (highmarks is not None and highmark not in highmarks):
            continue
        occurrences[highmark]["835"].append({
            "claim_id": str(claim_id), "claim_sequence": sequence,
            "highmark": highmark, "internal": str(internal or "").strip(),
            "file_id": str(file_id), "filename": filename, "status": status,
            "arrived_at": uploaded_at,
        })

    qmir = _highmark_scope(
        MIRClaim.objects.filter(mir_file__client=client), highmarks,
        combined_field="claim_control_number",
    )
    for values in qmir.values_list(
        "id", "claim_sequence", "claim_control_number", "mir_file_id",
        "mir_file__mir_filename", "mir_file__status", "mir_file__converted_at",
    ).iterator(chunk_size=2000):
        claim_id, sequence, claim_control, file_id, filename, status, converted_at = values
        highmark, internal = _numbers(claim_control)
        if not highmark or (highmarks is not None and highmark not in highmarks):
            continue
        occurrences[highmark]["mir"].append({
            "claim_id": str(claim_id), "claim_sequence": sequence,
            "highmark": highmark, "internal": internal, "file_id": str(file_id),
            "filename": filename, "status": status, "arrived_at": converted_at,
        })

    qrecon = _highmark_scope(
        RECONClaim.objects.filter(client=client, recon_file__file_kind="RECON"), highmarks,
        combined_field="claim_control_number",
    )
    for values in qrecon.values_list(
        "id", "claim_sequence", "claim_control_number", "patient_control_number",
        "recon_file_id", "recon_file__original_filename", "recon_file__status", "recon_file__uploaded_at",
    ).iterator(chunk_size=2000):
        claim_id, sequence, claim_control, patient_control, file_id, filename, status, uploaded_at = values
        highmark, internal = _numbers(claim_control or patient_control)
        if not highmark or (highmarks is not None and highmark not in highmarks):
            continue
        occurrences[highmark]["recon"].append({
            "claim_id": str(claim_id), "claim_sequence": sequence,
            "highmark": highmark, "internal": internal, "file_id": str(file_id),
            "filename": filename, "status": status, "arrived_at": uploaded_at,
        })

    for sources in occurrences.values():
        for source_type in sources:
            sources[source_type].sort(key=_occurrence_time_key)
    return occurrences


def _new_row(highmark, source_type, occurrence):
    return {
        "highmark": highmark,
        "internal": "",
        "match_internal": "",
        "837": occurrence if source_type == "837" else None,
        "835": occurrence if source_type == "835" else None,
        "mir": occurrence if source_type == "mir" else None,
        "recon": occurrence if source_type == "recon" else None,
    }


def _assemble_highmark_rows(highmark, sources):
    """Chronologically pair one occurrence per source according to source priority."""
    rows = [_new_row(highmark, "837", occurrence) for occurrence in sources["837"]]

    # 835 assigns the authoritative internal claim number. Pair oldest 835 with
    # oldest 837 for the same Highmark number, then create extra 835-only rows.
    for index, occurrence in enumerate(sources["835"]):
        if index < len(rows):
            row = rows[index]
        else:
            row = _new_row(highmark, "835", occurrence)
            rows.append(row)
        row["835"] = occurrence
        row["internal"] = occurrence["internal"]
        row["match_internal"] = occurrence["internal"].upper()

    def attach(source_type):
        for occurrence in sources[source_type]:
            wanted = str(occurrence.get("internal") or "").strip().upper()
            # MIR/RECON may join a row only after 835 established the internal
            # number. This prevents a value found only in 837 from being treated
            # as an internal claim number.
            candidates = [
                row for row in rows
                if row.get("835") is not None
                and row.get("match_internal")
                and row.get("match_internal") == wanted
                and row.get(source_type) is None
            ]
            if candidates and wanted:
                candidates[0][source_type] = occurrence
            else:
                rows.append(_new_row(highmark, source_type, occurrence))

    attach("mir")
    attach("recon")
    return rows


def _row_timestamp(row):
    for source_type in ("837", "835", "mir", "recon"):
        source = row.get(source_type)
        if source and source.get("arrived_at"):
            return source["arrived_at"]
    return None


def _row_matches(row, matched):
    if matched is None:
        return True
    for source_type in ("837", "835", "mir", "recon"):
        source = row.get(source_type)
        if source and source.get("claim_id") in matched[source_type]:
            return True
    return False


def _source_payload(source, expose_internal=True):
    if not source:
        return {
            "exists": False, "file_name": "", "arrived_at": None,
            "status": "", "internal_claim_number": "", "file_id": "",
        }
    return {
        "exists": True,
        "file_name": source.get("filename") or "",
        "arrived_at": source["arrived_at"].isoformat() if source.get("arrived_at") else None,
        "status": source.get("status") or "",
        "internal_claim_number": source.get("internal") if expose_internal else "",
        "file_id": source.get("file_id") or "",
    }


def _serialize_row(row):
    timestamp = _row_timestamp(row)
    primary = next((row.get(kind) for kind in ("837", "835", "mir", "recon") if row.get(kind)), None)
    primary_type = next((kind for kind in ("837", "835", "mir", "recon") if row.get(kind)), "claim")
    return {
        "id": f"occ:{primary_type}:{primary.get('claim_id') if primary else row['highmark']}",
        "highmark_claim_number": row["highmark"],
        # Displayed internal claim number is sourced only from 835.
        "internal_claim_number": row.get("internal") if row.get("835") else "",
        "row_arrived_at": timestamp.isoformat() if timestamp else None,
        "has_837": bool(row.get("837")),
        "lifecycle": {
            "837": _source_payload(row.get("837"), expose_internal=False),
            "835": _source_payload(row.get("835"), expose_internal=True),
            "mir": _source_payload(row.get("mir"), expose_internal=True),
            "recon": _source_payload(row.get("recon"), expose_internal=True),
        },
    }


@authenticated_api_required
@json_api_errors
def edi837_search(request):
    """Browse/search occurrence rows with 837 -> 835 -> MIR -> RECON matching."""
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
            "success": True, "query": query, "field": field, "count": 0,
            "results": [], "page": 1, "page_size": page_size, "pages": 0,
            "has_previous": False, "has_next": False,
            "search_mode": "too_short", "minimum_characters": MIN_PARTIAL_DIGITS,
        })

    partial_numeric = bool(
        field in {"all", "highmark", "internal"}
        and re.fullmatch(rf"\d{{{MIN_PARTIAL_DIGITS},{MAX_PARTIAL_DIGITS}}}", query)
    )
    matched, candidate_highmarks = _matched_source_ids(client, query, field, partial_numeric)
    if query and not candidate_highmarks:
        return JsonResponse({
            "success": True, "query": query, "field": field, "count": 0,
            "results": [], "page": 1, "page_size": page_size, "pages": 0,
            "has_previous": False, "has_next": False,
            "search_mode": "occurrence_backend",
        })

    occurrences = _load_occurrences(client, candidate_highmarks if query else None)
    rows = []
    for highmark, sources in occurrences.items():
        rows.extend(
            row for row in _assemble_highmark_rows(highmark, sources)
            if _row_matches(row, matched)
        )

    # Pairing is chronological, but the operational screen shows newest claim
    # arrivals first. Stable secondary keys make pagination deterministic.
    def sort_key(row):
        stamp = _row_timestamp(row)
        numeric_stamp = stamp.timestamp() if stamp else 0
        primary = next((row.get(kind) for kind in ("837", "835", "mir", "recon") if row.get(kind)), {})
        return (numeric_stamp, row["highmark"], str(primary.get("claim_id") or ""))

    rows.sort(key=sort_key, reverse=True)
    paginator = Paginator(rows, page_size)
    page_obj = paginator.get_page(page_number)
    serialized = [_serialize_row(row) for row in page_obj.object_list]

    return JsonResponse({
        "success": True,
        "query": query,
        "field": field,
        "count": paginator.count,
        "results": serialized,
        "page": page_obj.number,
        "page_size": page_size,
        "pages": paginator.num_pages if paginator.count else 0,
        "has_previous": page_obj.has_previous(),
        "has_next": page_obj.has_next(),
        "search_mode": "occurrence_backend",
    })
