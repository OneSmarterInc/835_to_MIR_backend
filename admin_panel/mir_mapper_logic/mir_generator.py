"""Fixed-width MIR record generator driven by editable mapping configuration."""
from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Tuple

from . import config
from .mapping_engine import evaluate_field
from .mapping_store import get_mappings
from .mir_mapper import claim_primary_reason
from .models import Claim, ServiceLine


def _put(buffer: List[str], field: dict, value: str) -> None:
    value = "" if value is None else str(value)
    length = int(field["length"])
    if len(value) > length:
        raise ValueError(
            f"MIR field {field.get('id', '(unknown)')} is {len(value)} characters; maximum is {length}"
        )
    pad = str(field.get("pad", " ") or " ")[:1]
    if field.get("align") == "right":
        value = value.rjust(length, pad)
    else:
        value = value.ljust(length, pad)
    start = int(field["start"]) - 1
    buffer[start:start + length] = list(value)


def _header(claim: Claim, sequence: int, max_sequence: int, line_count: int, fields: list[dict], process_date: date | None = None) -> str:
    b = [config.BLANK_CHAR] * config.MIR_HEADER_LENGTH
    for field in fields:
        if field.get("scope") == "Service":
            continue
        value = evaluate_field(field, claim, None, sequence, max_sequence, line_count, process_date=process_date)
        _put(b, field, value)
    result = "".join(b)
    if len(result) != config.MIR_HEADER_LENGTH:
        raise ValueError(f"Header generated with invalid length {len(result)}")
    return result


def _service_block(service: ServiceLine, claim: Claim, sequence: int, max_sequence: int,
                   line_count: int, inherited_reason: str, fields: list[dict], process_date: date | None = None) -> str:
    b = [config.BLANK_CHAR] * config.MIR_SERVICE_BLOCK_LENGTH
    for field in fields:
        if field.get("scope") != "Service":
            continue
        value = evaluate_field(field, claim, service, sequence, max_sequence, line_count, inherited_reason, process_date=process_date)
        _put(b, field, value)
    result = "".join(b)
    if len(result) != config.MIR_SERVICE_BLOCK_LENGTH:
        raise ValueError(f"Service block generated with invalid length {len(result)}")
    return result


def generate_mir_records(claims: Iterable[Claim], client=None, process_date: date | None = None) -> Tuple[List[str], Dict[str, int]]:
    records: List[str] = []
    total_claims = 0
    total_services = 0
    split_claims = 0
    output_bytes = 0
    fields = get_mappings(client)

    for claim in claims:
        total_claims += 1
        services = claim.services or []
        total_services += len(services)

        if config.SERVICE_OVERFLOW_MODE == "truncate":
            chunks = [services[:config.MAX_SERVICE_LINES_PER_RECORD]] if services else [[]]
        elif config.SERVICE_OVERFLOW_MODE == "split":
            chunks = [services[i:i + config.MAX_SERVICE_LINES_PER_RECORD]
                      for i in range(0, len(services), config.MAX_SERVICE_LINES_PER_RECORD)] or [[]]
        else:
            raise ValueError(
                f"Unsupported SERVICE_OVERFLOW_MODE={config.SERVICE_OVERFLOW_MODE!r}; "
                "use 'split' or 'truncate'."
            )

        max_sequence = len(chunks)
        if max_sequence > 1:
            split_claims += 1
        if max_sequence > config.MAX_RECORD_SEQUENCE:
            raise ValueError(
                f"Claim {claim.claim_number} requires {max_sequence} MIR records; "
                f"configured maximum is {config.MAX_RECORD_SEQUENCE}."
            )

        inherited_reason = claim_primary_reason(claim)
        for sequence, chunk in enumerate(chunks, start=1):
            if config.SERVICE_OVERFLOW_MODE == "truncate" and len(services) > config.MAX_SERVICE_LINES_PER_RECORD:
                header_service_count = len(chunk)
            else:
                header_service_count = len(chunk)
            record = _header(claim, sequence, max_sequence, header_service_count, fields, process_date)
            record += "".join(
                _service_block(svc, claim, sequence, max_sequence, header_service_count, inherited_reason, fields, process_date)
                for svc in chunk
            )
            expected = config.MIR_HEADER_LENGTH + len(chunk) * config.MIR_SERVICE_BLOCK_LENGTH
            if len(record) != expected:
                raise ValueError(
                    f"Claim {claim.claim_number} record {sequence}: expected length {expected}, got {len(record)}"
                )
            output_bytes += len(record) + 2
            if output_bytes > config.MAX_OUTPUT_BYTES:
                raise ValueError(f"Generated MIR exceeds the {config.MAX_OUTPUT_BYTES} byte limit")
            records.append(record)

    return records, {
        "claims": total_claims,
        "services": total_services,
        "mir_records": len(records),
        "split_claims": split_claims,
    }


def generate_mir_text(claims: Iterable[Claim], client=None, process_date: date | None = None) -> Tuple[str, Dict[str, int]]:
    records, summary = generate_mir_records(claims, client, process_date)
    text = "\r\n".join(records)
    if records:
        text += "\r\n"
    return text, summary
