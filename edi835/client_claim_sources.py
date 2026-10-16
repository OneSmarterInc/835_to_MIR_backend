"""Resolve exact claim records and produce source-specific, single-claim slices."""
import csv
import io
import re
from collections.abc import Iterable

from django.db.models import Q

from .client_claim_slice import claim_slice
from .models import EDI835Claim, EDI837Claim, MIRClaim, RECONClaim
from .recon_service import _detect_delimiter, ALIASES, _key


ClaimRecord = EDI835Claim | EDI837Claim | MIRClaim | RECONClaim


def source_records(claim: EDI835Claim, kind: str) -> Iterable[ClaimRecord]:
    """Never substitute another 835 or an unrelated conversion's MIR output."""
    number = claim.highmark_claim_number.strip()
    internal = claim.internal_claim_number.strip()
    keys = {number}
    if internal:
        keys.update({number + internal, number + " " + internal})
        if len(number) <= 17 and len(internal) <= 6:
            keys.add((number.ljust(17) + internal.ljust(6)).strip())
    client_id = claim.edi_file.client_id
    if kind == "835":
        return [claim]
    if kind == "mir":
        return MIRClaim.objects.select_related("mir_file").defer("mir_file__file_content", "segment_data").filter(
            mir_file__source_835_id=claim.edi_file_id, mir_file__client_id=client_id,
            claim_control_number__in=keys,
        ).order_by("claim_sequence")
    if kind == "837":
        query = EDI837Claim.objects.select_related("edi_file").defer("edi_file__file_content", "raw_claim", "segment_data").filter(
            Q(highmark_claim_number=number) | Q(claim_control_number__in=keys),
            client_id=client_id, edi_file__client_id=client_id,
        )
        if internal:
            query = query.filter(internal_claim_number=internal)
        return query.order_by("edi_file__uploaded_at", "edi_file_id", "claim_sequence")
    if kind == "recon":
        return RECONClaim.objects.select_related("recon_file").defer("recon_file__file_content", "raw_record", "segment_data").filter(
            client_id=client_id, recon_file__client_id=client_id,
            recon_file__file_kind="RECON", claim_control_number__in=keys,
        ).order_by("recon_file__uploaded_at", "recon_file_id", "claim_sequence")
    return []


def source_metadata(record: ClaimRecord, kind: str) -> dict:
    source = (record.mir_file if kind == "mir" else
              record.recon_file if kind == "recon" else record.edi_file)
    return {
        "record_id": record.pk, "file_id": str(source.pk),
        "filename": source.mir_filename if kind == "mir" else source.original_filename,
        "received_at": (source.converted_at if kind == "mir" else source.uploaded_at).isoformat(),
        "sequence": record.claim_sequence,
    }


def available_sources(claim: EDI835Claim) -> list[dict]:
    result = []
    for kind in ("835", "mir", "837", "recon"):
        records = [source_metadata(record, kind) for record in source_records(claim, kind)]
        if records:
            result.append({"kind": kind, "linked": kind in {"835", "mir"}, "records": records})
    return result


def slice_837(claim: EDI837Claim) -> str:
    """Keep one CLM occurrence, its HL ancestors and its own interchange."""
    source = claim.edi_file.file_content.lstrip("\ufeff\r\n ")
    if not source.startswith("ISA") or len(source) < 106:
        raise ValueError("The archived 837 has no complete ISA envelope.")
    element, terminator = source[3], source[105]
    segments = [part.strip() for part in source.split(terminator) if part.strip()]
    isa = group = tx = None
    candidates = []
    for segment in segments:
        fields = segment.split(element)
        tag = fields[0]
        if tag == "ISA":
            isa = {"head": fields, "tail": None}
            group = tx = None
        elif tag == "GS":
            group = {"head": fields, "tail": None, "isa": isa}
            tx = None
        elif tag == "ST":
            tx = {"body": [segment], "tail": None, "group": group}
        elif tag == "SE":
            if tx:
                tx["tail"] = fields
            tx = None
        elif tag == "GE":
            if group:
                group["tail"] = fields
            group = tx = None
        elif tag == "IEA":
            if isa:
                isa["tail"] = fields
            isa = group = tx = None
        elif tx:
            if tag == "CLM" and tx["body"][0].split(element)[1] == "837":
                candidates.append((tx, len(tx["body"])))
            tx["body"].append(segment)
    try:
        if claim.claim_sequence < 1:
            raise ValueError("Invalid 837 claim occurrence.")
        tx, start = candidates[claim.claim_sequence - 1]
        body = tx["body"]
        fields = [segment.split(element) for segment in body]
        if fields[start][1].strip() != claim.claim_control_number.strip():
            raise ValueError("Claim occurrence does not match the archived 837.")
        group, st = tx["group"], fields[0]
        isa = group["isa"]
        if (len(isa["head"]) != 17 or group["head"][1] != "HC" or not st[2]
                or tx["tail"][2] != st[2]
                or group["tail"][2] != group["head"][6]
                or isa["tail"][2] != isa["head"][13]):
            raise ValueError("The archived 837 envelope controls do not match.")
        end = next((i for i in range(start + 1, len(body)) if fields[i][0] in {"CLM", "HL"}), len(body))
        hl_indices = [i for i, row in enumerate(fields) if row[0] == "HL"]
        if not hl_indices:
            raise ValueError("The selected 837 claim has no HL context.")
        nodes = {fields[i][1]: i for i in hl_indices}
        if len(nodes) != len(hl_indices):
            raise ValueError("The archived 837 has duplicate HL identifiers.")
        current = next((i for i in reversed(hl_indices) if i < start), None)
        chain = []
        while current is not None:
            if current in chain:
                raise ValueError("The archived 837 HL hierarchy is circular.")
            chain.append(current)
            parent = fields[current][2]
            if parent and parent not in nodes:
                raise ValueError("The archived 837 is missing a parent HL.")
            current = nodes.get(parent)
        if not chain:
            raise ValueError("The selected 837 claim has no parent HL.")
        chain.reverse()
        if chain != sorted(chain):
            raise ValueError("The archived 837 HL hierarchy is out of order.")
        keep = set(range(hl_indices[0])) | set(range(start, end))
        for index in chain:
            # Stop at the FIRST CLM even for an ancestor: never copy siblings.
            boundary = next((i for i in range(index + 1, len(body)) if fields[i][0] in {"HL", "CLM"}), len(body))
            keep.update(range(index, boundary))
        if sum(fields[i][0] == "CLM" for i in keep) != 1:
            raise ValueError("Unable to isolate one 837 claim.")
        for position, index in enumerate(chain, start=1):
            fields[index][1:3] = [str(position), str(position - 1) if position > 1 else ""]
            while len(fields[index]) < 5:
                fields[index].append("")
            fields[index][4] = "1" if position < len(chain) else "0"
        output = [element.join(fields[i]) for i in sorted(keep)]
        output.append(element.join(["SE", str(len(output) + 1), st[2]]))
        output = [element.join(isa["head"]), element.join(group["head"]), *output,
                  element.join(["GE", "1", group["head"][6]]),
                  element.join(["IEA", "1", isa["head"][13]])]
        return (terminator + "\n").join(output) + terminator + "\n"
    except (IndexError, KeyError, TypeError) as exc:
        raise ValueError("A complete matching 837 claim and envelope are unavailable.") from exc


def slice_mir(claim: MIRClaim) -> str:
    source = claim.mir_file.file_content
    archived = source.splitlines()
    rows = []
    chunks = list(claim.chunks.order_by("chunk_number"))
    if not chunks or len(chunks) != claim.chunk_count:
        raise ValueError("Complete MIR claim chunks are unavailable.")
    for position, chunk in enumerate(chunks, start=1):
        index = chunk.physical_row_number - 1
        if index < 0 or index >= len(archived) or archived[index] != chunk.raw_row:
            raise ValueError("The MIR claim no longer matches its archived source row.")
        row = archived[index]
        if len(row) < 334 or (len(row) - 334) % 303 or row[2:25].strip() != claim.claim_control_number:
            raise ValueError("The MIR claim row has an invalid format or identifier.")
        if (chunk.chunk_number != position or not row[332:334].strip().isdigit()
                or int(row[332:334]) != (len(row) - 334) // 303):
            raise ValueError("The MIR claim chunk order or service count is invalid.")
        rows.append(row)
    ending = "\r\n" if "\r\n" in source else "\n"
    return ending.join(rows) + ending


def slice_recon(claim: RECONClaim) -> str:
    """Read ORIGINAL rows, preserving CSV quotes and fixed-width padding."""
    source = claim.recon_file.file_content
    text = source.replace("\x1a", "")
    delimiter = _detect_delimiter(text)
    header = ""
    if delimiter:
        physical = text.splitlines(keepends=True)
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows, values, previous = [], [], 0
        for value in reader:
            rows.append("".join(physical[previous:reader.line_num]).rstrip("\r\n"))
            values.append(value)
            previous = reader.line_num
        if values and {_key(v) for v in values[0]} & set().union(*ALIASES.values()):
            header = rows[0]
    else:
        rows = [row for row in text.splitlines() if row.strip()]
    stored_rows = list(claim.service_lines.values_list("source_row_number", "raw_service"))
    numbers = sorted({number for number, _ in stored_rows})
    if not numbers or any(n < 1 or n > len(rows) or (header and n == 1) for n in numbers):
        raise ValueError("Original Recon source rows are unavailable for this claim.")
    for number, stored in stored_rows:
        original = delimiter.join(values[number - 1]) if delimiter else rows[number - 1]
        if original != stored:
            raise ValueError("The Recon claim no longer matches its archived source row.")
    if claim.recon_file.service_lines.filter(source_row_number__in=numbers).exclude(recon_claim=claim).exists():
        raise ValueError("A Recon source row belongs to multiple claims; it cannot be sliced safely.")
    ending = "\r\n" if "\r\n" in source else "\n"
    output = ending.join(([header] if header else []) + [rows[n - 1] for n in numbers]) + ending
    return output + ("\x1a" if source.endswith("\x1a") else "")


def source_slice(record: ClaimRecord, kind: str) -> str:
    if kind == "835":
        sliced = claim_slice(record)
        if sliced["error"]:
            raise ValueError(sliced["error"])
        return sliced["content"]
    return {"mir": slice_mir, "837": slice_837, "recon": slice_recon}[kind](record)


def download_name(claim: EDI835Claim, kind: str) -> str:
    number = re.sub(r"[^A-Za-z0-9_-]", "_", claim.highmark_claim_number).strip("_")[:100]
    return f"{number or ('claim-' + str(claim.pk))}.{kind}"
