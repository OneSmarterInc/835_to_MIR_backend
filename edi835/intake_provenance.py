"""Transaction-level lineage without copying claim payloads into audit tables."""
import hashlib
import json

from .models import IntakeTransactionSet


def record_transactions(receipt, raw):
    from .client_portal_analysis import analysis_segments

    rows = []
    current = None
    version = ""
    try:
        for position, (fields, _, _) in enumerate(analysis_segments(raw.decode("utf-8-sig").strip()), 1):
            if fields[0] == "GS":
                version = fields[8] if len(fields) > 8 else ""
            if fields[0] == "ST":
                if current is not None or len(fields) < 3 or len(fields[1]) != 3 or not fields[1].isdigit():
                    return
                current = (position, fields, hashlib.sha256())
            if current is not None:
                current[2].update(json.dumps(fields, separators=(",", ":")).encode() + b"\n")
            if fields[0] == "SE":
                if current is None or len(fields) < 3 or fields[2] != current[1][2]:
                    return
                start, header, digest = current
                if any(len(value) > 100 for value in (header[2], version)):
                    return
                rows.append(IntakeTransactionSet(receipt=receipt, sequence=len(rows) + 1,
                    transaction_type=header[1], control_number=header[2],
                    version=(header[3] if len(header) > 3 else version)[:100],
                    first_segment=start, last_segment=position,
                    checksum=digest.hexdigest()))
                current = None
        if current is not None:
            return
    except (ValueError, UnicodeError):
        # The established validator determines acceptance. Never guess missing
        # source coordinates for legacy fragments or malformed envelopes.
        return
    IntakeTransactionSet.objects.bulk_create(rows, ignore_conflicts=True)
