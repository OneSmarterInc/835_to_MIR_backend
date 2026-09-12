"""Normalize stored 835 X12 content into claim-level database rows."""

import re
from decimal import Decimal, InvalidOperation

from .models import EDI835Claim


def _decimal(value):
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def normalized_835_claims(content):
    segments = [
        segment.strip()
        for segment in re.split(r"[~\r\n]+", str(content or ""))
        if segment.strip()
    ]
    claims = []
    current = None
    for segment in segments:
        fields = segment.split("*")
        tag = fields[0].upper()
        if tag == "CLP":
            if current:
                claims.append(current)
            highmark = fields[1].strip() if len(fields) > 1 else ""
            candidates = [value.strip() for value in fields[6:] if value.strip()]
            internal = next(
                (
                    value
                    for value in candidates
                    if value.upper() != highmark.upper()
                    and re.search(r"[A-Za-z]", value)
                    and re.search(r"\d", value)
                ),
                "",
            )
            current = {
                "highmark_claim_number": highmark,
                "internal_claim_number": internal,
                "claim_status": fields[2].strip() if len(fields) > 2 else "",
                "total_charge_amount": _decimal(fields[3] if len(fields) > 3 else 0),
                "paid_amount": _decimal(fields[4] if len(fields) > 4 else 0),
                "patient_responsibility": _decimal(fields[5] if len(fields) > 5 else 0),
                "service_count": 0,
                "segments": [segment],
            }
        elif current:
            current["segments"].append(segment)
            if tag == "SVC":
                current["service_count"] += 1
    if current:
        claims.append(current)
    return [claim for claim in claims if claim["highmark_claim_number"]]


def normalize_835_file(edi_file, *, replace=False):
    """Create idempotent normalized rows for one immutable stored 835 file."""
    if not edi_file.input_file_content:
        return 0
    if edi_file.claims.exists() and not replace:
        return edi_file.claims.count()
    rows = normalized_835_claims(edi_file.input_file_content)
    if replace:
        edi_file.claims.all().delete()
    EDI835Claim.objects.bulk_create([
        EDI835Claim(
            edi_file=edi_file,
            claim_sequence=index,
            highmark_claim_number=row["highmark_claim_number"],
            internal_claim_number=row["internal_claim_number"],
            claim_status=row["claim_status"],
            total_charge_amount=row["total_charge_amount"],
            paid_amount=row["paid_amount"],
            patient_responsibility=row["patient_responsibility"],
            service_count=row["service_count"],
            raw_claim="~".~".join(row["segments"]) + "~",
            segment_data={"segments": row["segments"]},
        )
        for index, row in enumerate(rows, start=1)
    ])
    return len(rows)
