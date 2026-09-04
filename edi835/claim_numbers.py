import re


def split_claim_number(value):
    """Split a combined claim key at its first alphabetic character."""
    claim = str(value or "").strip()
    match = re.match(r"^(\d+)([A-Za-z].*)$", claim)
    if match:
        return {
            "highmark_claim_number": match.group(1),
            "internal_claim_number": match.group(2),
        }
    return {"highmark_claim_number": "", "internal_claim_number": claim}


def add_split_claim_number(payload, value=None):
    payload.update(split_claim_number(payload.get("claim_id") if value is None else value))
    return payload
