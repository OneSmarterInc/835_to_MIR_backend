import re


def split_claim_number(value):
    """Return the canonical Highmark and internal portions of a claim key."""
    claim = str(value or "").strip()
    match = re.match(r"^(\d+)[^A-Za-z0-9]*([A-Za-z].*)$", claim)
    if match:
        return {
            "highmark_claim_number": match.group(1),
            "internal_claim_number": match.group(2),
        }
    if claim.isdigit():
        return {"highmark_claim_number": claim, "internal_claim_number": ""}
    return {"highmark_claim_number": "", "internal_claim_number": claim}


def add_split_claim_number(payload, value=None):
    payload.update(split_claim_number(payload.get("claim_id") if value is None else value))
    return payload
