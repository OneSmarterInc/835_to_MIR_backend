"""Generate claim-only downloads that remain valid for each source file type."""

import os
import re

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods

from admin_panel.access_control import can_access_client
from .claim_numbers import split_claim_number
from .models import EDI835File, EDI837File, MIRFile, RECONFile


FILE_MODELS = {
    "835": (EDI835File, "input_file_content", "original_filename"),
    "837": (EDI837File, "file_content", "original_filename"),
    "mir": (MIRFile, "file_content", "mir_filename"),
    "recon": (RECONFile, "file_content", "original_filename"),
}


def _safe_name(filename, claim_number):
    filename = os.path.basename(str(filename or "claim-file.txt")).replace('"', "")
    stem, extension = os.path.splitext(filename)
    extension = extension or ".txt"
    claim = re.sub(r"[^A-Za-z0-9_-]+", "_", str(claim_number or "claim"))
    return f"{stem}_claim_{claim}{extension}"


def _identity_values(claim_number, internal_number=""):
    values = {
        str(claim_number or "").strip().upper(),
        str(internal_number or "").strip().upper(),
    }
    values.discard("")
    return values


def _highmark_match(text, claim_number):
    wanted = str(claim_number or "").strip()
    if not wanted:
        return False
    return bool(re.search(rf"(?<!\d){re.escape(wanted)}(?!\d)", str(text or ""), re.I))


def _internal_match(text, internal_number):
    wanted = str(internal_number or "").strip()
    if not wanted:
        return True
    return wanted.upper() in str(text or "").upper()


def _split_x12(content):
    source = str(content or "").lstrip("\ufeff\r\n ")
    if not source:
        raise ValueError("The archived X12 file is empty.")
    element = source[3] if source.startswith("ISA") and len(source) > 3 else "*"
    segment = source[105] if source.startswith("ISA") and len(source) > 105 else "~"
    segments = [part.strip() for part in source.split(segment) if part.strip()]
    return element, segment, segments


def _tag(segment, element="*"):
    return str(segment or "").split(element, 1)[0].upper()


def _replace_elements(segment, element, replacements):
    fields = str(segment or "").split(element)
    for index, value in replacements.items():
        while len(fields) <= index:
            fields.append("")
        fields[index] = str(value)
    return element.join(fields)


def _transaction_ranges(segments, element):
    ranges = []
    start = None
    for index, segment in enumerate(segments):
        tag = _tag(segment, element)
        if tag == "ST":
            start = index
        elif tag == "SE" and start is not None:
            ranges.append((start, index))
            start = None
    return ranges


def _claim_loop_matches(loop, claim_number, internal_number):
    text = "~".join(loop)
    return _highmark_match(text, claim_number) and _internal_match(text, internal_number)


def _slice_835_transaction(transaction, element, claim_number, internal_number):
    if not transaction or _tag(transaction[0], element) != "ST":
        return None
    st_fields = transaction[0].split(element)
    if len(st_fields) < 2 or st_fields[1] != "835":
        return None

    body = transaction[1:-1]
    first_claimish = next((
        index for index, segment in enumerate(body)
        if _tag(segment, element) in {"LX", "CLP"}
    ), len(body))
    prefix = list(body[:first_claimish])
    region = body[first_claimish:]
    selected = []
    current_lx = None
    index = 0

    while index < len(region):
        tag = _tag(region[index], element)
        if tag == "PLB":
            break
        if tag == "LX":
            current_lx = region[index]
            index += 1
            continue
        if tag != "CLP":
            index += 1
            continue

        end = index + 1
        while end < len(region) and _tag(region[end], element) not in {"CLP", "LX", "PLB"}:
            end += 1
        loop = region[index:end]
        if _claim_loop_matches(loop, claim_number, internal_number):
            if current_lx and (not selected or selected[-1] != current_lx):
                selected.append(current_lx)
            selected.extend(loop)
        index = end

    if not selected:
        return None
    output = [transaction[0], *prefix, *selected]
    st_control = st_fields[2] if len(st_fields) > 2 else "0001"
    se = transaction[-1] if transaction and _tag(transaction[-1], element) == "SE" else "SE"
    se = _replace_elements(se, element, {1: len(output) + 1, 2: st_control})
    output.append(se)
    return output


def _hl_info(segment, element):
    fields = segment.split(element)
    return {
        "id": fields[1] if len(fields) > 1 else "",
        "parent": fields[2] if len(fields) > 2 else "",
        "level": fields[3] if len(fields) > 3 else "",
    }


def _slice_837_transaction(transaction, element, claim_number, internal_number):
    if not transaction or _tag(transaction[0], element) != "ST":
        return None
    st_fields = transaction[0].split(element)
    if len(st_fields) < 2 or st_fields[1] != "837":
        return None

    body = transaction[1:-1]
    hl_indices = [index for index, segment in enumerate(body) if _tag(segment, element) == "HL"]
    first_hl = hl_indices[0] if hl_indices else len(body)
    keep = set(range(first_hl))

    hl_by_id = {}
    for position, index in enumerate(hl_indices):
        info = _hl_info(body[index], element)
        next_hl = hl_indices[position + 1] if position + 1 < len(hl_indices) else len(body)
        hl_by_id[info["id"]] = {**info, "index": index, "next": next_hl}

    target_claims = []
    for index, segment in enumerate(body):
        if _tag(segment, element) != "CLM":
            continue
        end = index + 1
        while end < len(body) and _tag(body[end], element) not in {"CLM", "HL"}:
            end += 1
        loop = body[index:end]
        if _claim_loop_matches(loop, claim_number, internal_number):
            target_claims.append((index, end))

    if not target_claims:
        return None

    for claim_start, claim_end in target_claims:
        containing_hl = next((index for index in reversed(hl_indices) if index < claim_start), None)
        if containing_hl is not None:
            current_info = _hl_info(body[containing_hl], element)
            chain = []
            seen = set()
            node_id = current_info["id"]
            while node_id and node_id not in seen and node_id in hl_by_id:
                seen.add(node_id)
                node = hl_by_id[node_id]
                chain.append(node)
                node_id = node["parent"]
            for node in reversed(chain):
                start = node["index"]
                end = node["next"]
                if start == containing_hl:
                    first_clm = next((
                        pos for pos in range(start + 1, end)
                        if _tag(body[pos], element) == "CLM"
                    ), end)
                    end = first_clm
                keep.update(range(start, end))
        keep.update(range(claim_start, claim_end))

    selected_body = [body[index] for index in sorted(keep)]
    output = [transaction[0], *selected_body]
    st_control = st_fields[2] if len(st_fields) > 2 else "0001"
    se = transaction[-1] if transaction and _tag(transaction[-1], element) == "SE" else "SE"
    se = _replace_elements(se, element, {1: len(output) + 1, 2: st_control})
    output.append(se)
    return output


def _slice_x12(content, transaction_type, claim_number, internal_number):
    element, segment_sep, segments = _split_x12(content)
    isa = next((item for item in segments if _tag(item, element) == "ISA"), None)
    iea = next((item for item in reversed(segments) if _tag(item, element) == "IEA"), None)
    if not isa or not iea:
        raise ValueError("The archived X12 file does not contain a complete ISA/IEA envelope.")

    group_outputs = []
    index = 0
    while index < len(segments):
        if _tag(segments[index], element) != "GS":
            index += 1
            continue
        gs_index = index
        ge_index = next((
            pos for pos in range(gs_index + 1, len(segments))
            if _tag(segments[pos], element) == "GE"
        ), None)
        if ge_index is None:
            break
        group = segments[gs_index:ge_index + 1]
        transactions = []
        for start, end in _transaction_ranges(group, element):
            tx = group[start:end + 1]
            sliced = (
                _slice_835_transaction(tx, element, claim_number, internal_number)
                if transaction_type == "835"
                else _slice_837_transaction(tx, element, claim_number, internal_number)
            )
            if sliced:
                transactions.append(sliced)
        if transactions:
            gs = group[0]
            gs_fields = gs.split(element)
            gs_control = gs_fields[6] if len(gs_fields) > 6 else "1"
            ge = group[-1]
            ge = _replace_elements(ge, element, {1: len(transactions), 2: gs_control})
            flattened = [gs]
            for tx in transactions:
                flattened.extend(tx)
            flattened.append(ge)
            group_outputs.append(flattened)
        index = ge_index + 1

    if not group_outputs:
        raise ValueError("No matching claim transaction was found in this archived X12 file.")

    isa_fields = isa.split(element)
    isa_control = isa_fields[13] if len(isa_fields) > 13 else "1"
    iea = _replace_elements(iea, element, {1: len(group_outputs), 2: isa_control})
    output = [isa]
    for group in group_outputs:
        output.extend(group)
    output.append(iea)
    return segment_sep.join(output) + segment_sep


def _mir_slice(record, claim_number, internal_number):
    rows = []
    claims = record.claims.prefetch_related("chunks").order_by("claim_sequence")
    for claim in claims:
        text = f"{claim.claim_control_number} {claim.header_raw}"
        if not _highmark_match(text, claim_number) or not _internal_match(text, internal_number):
            continue
        chunks = [chunk.raw_row for chunk in claim.chunks.order_by("chunk_number") if chunk.raw_row]
        rows.extend(chunks or ([claim.header_raw] if claim.header_raw else []))
    if not rows:
        raise ValueError("No matching MIR claim record was found.")
    line_end = "\r\n" if "\r\n" in str(record.file_content or "") else "\n"
    return line_end.join(row.rstrip("\r\n") for row in rows) + line_end


def _recon_header(content):
    lines = str(content or "").replace("\x1a", "").splitlines()
    if not lines:
        return ""
    first = lines[0]
    delimiter = next((value for value in (",", "\t", "|", ";") if value in first), None)
    if not delimiter:
        return ""
    normalized = re.sub(r"[^a-z0-9]", "", first.lower())
    return first if any(token in normalized for token in ("claim", "member", "patient", "status", "paid", "charge")) else ""


def _recon_slice(record, claim_number, internal_number):
    rows = []
    claims = record.claims.prefetch_related("service_lines").order_by("claim_sequence")
    for claim in claims:
        text = f"{claim.claim_control_number} {claim.patient_control_number} {claim.raw_record}"
        if not _highmark_match(text, claim_number) or not _internal_match(text, internal_number):
            continue
        seen_row_numbers = set()
        service_rows = []
        for service in claim.service_lines.order_by("source_row_number", "service_sequence"):
            if service.source_row_number in seen_row_numbers:
                continue
            seen_row_numbers.add(service.source_row_number)
            if service.raw_service:
                service_rows.append(service.raw_service)
        if service_rows:
            rows.extend(service_rows)
        elif claim.raw_record:
            rows.extend(claim.raw_record.splitlines())
    if not rows:
        raise ValueError("No matching RECON claim record was found.")
    header = _recon_header(record.file_content)
    output_rows = ([header] if header else []) + rows
    line_end = "\r\n" if "\r\n" in str(record.file_content or "") else "\n"
    output = line_end.join(row.rstrip("\r\n") for row in output_rows) + line_end
    if str(record.file_content or "").endswith("\x1a"):
        output += "\x1a"
    return output


@require_http_methods(["GET"])
def mpl_claim_slice(request, file_type, file_id):
    config = FILE_MODELS.get(str(file_type or "").lower())
    if not config:
        return JsonResponse({"success": False, "error": "Unsupported file type."}, status=404)
    model, content_field, filename_field = config
    record = model.objects.filter(pk=file_id).first()
    if not record:
        return JsonResponse({"success": False, "error": "File not found."}, status=404)
    if not can_access_client(request.user, record.client_id):
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)

    claim_number = str(request.GET.get("claim_number") or "").strip()
    internal_number = str(request.GET.get("internal_claim_number") or "").strip()
    if not claim_number:
        return JsonResponse({"success": False, "error": "Claim number is required."}, status=400)

    try:
        normalized_type = str(file_type).lower()
        content = getattr(record, content_field, "") or ""
        if normalized_type in {"835", "837"}:
            sliced = _slice_x12(content, normalized_type, claim_number, internal_number)
        elif normalized_type == "mir":
            sliced = _mir_slice(record, claim_number, internal_number)
        else:
            sliced = _recon_slice(record, claim_number, internal_number)
    except ValueError as exc:
        return JsonResponse({"success": False, "error": str(exc)}, status=404)

    filename = _safe_name(getattr(record, filename_field, "claim-file.txt"), claim_number)
    response = HttpResponse(sliced.encode("utf-8"), content_type="application/octet-stream")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response
