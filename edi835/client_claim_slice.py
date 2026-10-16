"""Select one persisted claim occurrence and rebuild its 835 envelope."""
from decimal import Decimal, InvalidOperation
import re


def claim_slice(claim) -> dict:
    source = str(claim.edi_file.input_file_content or "").lstrip("\ufeff\r\n ")
    archived = bool(source)
    if not source:
        stored = (claim.segment_data or {}).get("segments") or []
        source = "~".join(item for item in stored if isinstance(item, str)) or claim.raw_claim
    element = source[3] if source.startswith("ISA") and len(source) > 3 else "*"
    delimiter = source[105] if source.startswith("ISA") and len(source) > 105 else "~"
    segments = [s.strip() for s in (source.split(delimiter) if delimiter in source
                                    else re.split(r"[\r\n]+", source)) if s.strip()]
    isa = group = transaction = current = None
    candidates = []
    for segment in segments:
        fields = segment.split(element)
        tag = fields[0].upper()
        if tag == "ISA":
            isa = {"head": fields, "tail": None}
            group = transaction = current = None
        elif tag == "GS":
            group = {"head": fields, "tail": None, "isa": isa}
            transaction = current = None
        elif tag == "ST":
            transaction = {"head": [segment], "tail": None, "group": group,
                           "prefix": True, "lx": None}
            current = None
        elif tag == "SE":
            if transaction:
                transaction["tail"] = fields
            transaction = current = None
        elif tag == "GE":
            if group:
                group["tail"] = fields
            group = transaction = current = None
        elif tag == "IEA":
            if isa:
                isa["tail"] = fields
            isa = group = transaction = current = None
        elif tag in {"LX", "PLB", "TS3", "TS2"}:
            current = None
            if transaction:
                transaction["prefix"] = False
                if tag == "LX":
                    transaction["lx"] = segment
        elif tag == "CLP":
            current = {"segments": [segment], "transaction": transaction,
                       "lx": transaction["lx"] if transaction else None}
            if len(fields) > 1 and fields[1].strip():
                candidates.append(current)
            if transaction:
                transaction["prefix"] = False
        elif current:
            current["segments"].append(segment)
        elif transaction and transaction["prefix"]:
            transaction["head"].append(segment)

    wanted = str(claim.highmark_claim_number).strip()
    matches = [item for item in candidates if item["segments"][0].split(element)[1].strip() == wanted]
    ordinal = claim.claim_sequence - 1
    selected = candidates[ordinal] if archived and 0 <= ordinal < len(candidates) else None
    if selected not in matches:
        selected = matches[0] if len(matches) == 1 else None
    result = {"claim_segments": [], "segments": [], "content": "", "error": "",
              "element": element}
    if selected is None:
        result["error"] = "The selected claim occurrence could not be identified safely in the archived 835."
        return result
    result["claim_segments"] = selected["segments"]
    try:
        tx = selected["transaction"]
        group = tx["group"]
        isa = group["isa"]
        st = tx["head"][0].split(element)
        if (st[1] != "835" or not st[2] or tx["tail"][2] != st[2]
                or len(isa["head"]) != 17 or group["head"][1] != "HP"
                or group["tail"][2] != group["head"][6]
                or isa["tail"][2] != isa["head"][13]):
            raise ValueError("Invalid envelope")
        paid = Decimal(selected["segments"][0].split(element)[4])
        if not paid.is_finite():
            raise ValueError("Invalid claim payment")
        header = []
        payment_seen = False
        for segment in tx["head"]:
            fields = segment.split(element)
            if fields[0] == "BPR":
                if len(fields) < 3:
                    raise ValueError("Incomplete BPR")
                fields[2] = format(paid, ".2f")
                payment_seen = True
            header.append(element.join(fields))
        if not payment_seen:
            raise ValueError("Missing BPR")
        body = header + ([selected["lx"]] if selected["lx"] else []) + selected["segments"]
        body.append(element.join(["SE", str(len(body) + 1), st[2]]))
        output = [element.join(isa["head"]), element.join(group["head"]), *body,
                  element.join(["GE", "1", group["head"][6]]),
                  element.join(["IEA", "1", isa["head"][13]])]
        result["segments"] = output
        result["content"] = (delimiter + "\n").join(output) + delimiter + "\n"
    except (TypeError, KeyError, IndexError, ValueError, InvalidOperation):
        result["error"] = "A complete, matching ISA/GS/ST and SE/GE/IEA envelope is unavailable for this claim."
    return result
