"""Fixed-width MIR record generator driven by editable mapping configuration."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Tuple

from . import config
from .mapping_engine import evaluate_field
from .mapping_store import get_mappings
from .mir_mapper import claim_primary_reason
from .mir_mapper import co_adjustment_total, covered_charge, patient_liability
from .models import Claim, ServiceLine
from .rule_registry import (
    MPL_SOURCE,
    RuleSeverity,
    claim_control_number,
    evaluate_preventive_rules,
    is_blocking,
)


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


def _header(claim: Claim, sequence: int, max_sequence: int, line_count: int,
            fields: list[dict], process_date: date | None = None) -> str:
    b = [config.BLANK_CHAR] * config.MIR_HEADER_LENGTH
    for field in fields:
        if field.get("scope") == "Service":
            continue
        value = evaluate_field(
            field, claim, None, sequence, max_sequence, line_count, process_date=process_date
        )
        _put(b, field, value)
    result = "".join(b)
    if len(result) != config.MIR_HEADER_LENGTH:
        raise ValueError(f"Header generated with invalid length {len(result)}")
    return result


def _service_block(service: ServiceLine, claim: Claim, sequence: int, max_sequence: int,
                   line_count: int, inherited_reason: str, fields: list[dict],
                   process_date: date | None = None) -> str:
    b = [config.BLANK_CHAR] * config.MIR_SERVICE_BLOCK_LENGTH
    for field in fields:
        if field.get("scope") != "Service":
            continue
        value = evaluate_field(
            field, claim, service, sequence, max_sequence, line_count,
            inherited_reason, process_date=process_date,
        )
        _put(b, field, value)
    result = "".join(b)
    if len(result) != config.MIR_SERVICE_BLOCK_LENGTH:
        raise ValueError(f"Service block generated with invalid length {len(result)}")
    return result


def _finding(claim: Claim, code: str, reason: str, service_line: int | None = None,
             severity: str = RuleSeverity.HOLD.value, **details: Any) -> dict:
    source = MPL_SOURCE if code.startswith("MP") else "OneSmarter MIR generation control"
    finding = {
        "rule_code": code,
        "rule_name": code,
        "source": source,
        "severity": severity,
        "scope": "service" if service_line is not None else "claim",
        "claim_number": claim.claim_number,
        "claim_control_number": claim_control_number(claim),
        "service_line": service_line,
        "reason": reason,
        "provenance": {
            "rule_code": code,
            "source": source,
            "description": reason,
        },
    }
    finding.update({key: str(value) for key, value in details.items() if value is not None})
    return finding


def _claim_findings(claim: Claim) -> list[dict]:
    findings: list[dict] = []
    for line_number, service in enumerate(claim.services or [], start=1):
        contractual = co_adjustment_total(service)
        covered = covered_charge(service)
        liability = patient_liability(service)
        if contractual > service.charge:
            findings.append(_finding(
                claim, "CO_EXCEEDS_CHARGE",
                "Contractual adjustments exceed the service charge.", line_number,
                service_charge=service.charge, contractual_adjustments=contractual,
            ))
        if covered < Decimal("0"):
            findings.append(_finding(
                claim, "NEGATIVE_COVERED_CHARGE",
                "Derived covered charge is negative.", line_number,
                covered_charge=covered,
            ))
        if liability < Decimal("0"):
            findings.append(_finding(
                claim, "MP003",
                "Payment exceeds the derived covered amount.", line_number,
                patient_liability=liability, payment=service.paid,
            ))
        for adjustment in service.adjustments:
            if (
                adjustment.group == config.X12_PATIENT_RESP_GROUP
                and adjustment.reason not in config.ORDINARY_PATIENT_RESPONSIBILITY_REASONS
                and adjustment.reason != "45"
            ):
                findings.append(_finding(
                    claim, "UNMAPPED_PR_REASON",
                    f"Patient-responsibility reason PR{adjustment.reason} has no MIR reduction slot.",
                    line_number, adjustment_reason=f"PR{adjustment.reason}",
                    adjustment_amount=adjustment.amount,
                ))
    return findings


def _persisted_icns(claims: list[Claim], client=None) -> set[str]:
    """Return previously generated full ICNs for this tenant without changing storage."""
    if client is None:
        return set()
    incoming = {claim_control_number(claim) for claim in claims if claim_control_number(claim)}
    if not incoming:
        return set()
    from edi835.models import MIRClaim

    return set(
        MIRClaim.objects.filter(
            mir_file__client=client,
            claim_control_number__in=incoming,
        ).values_list("claim_control_number", flat=True)
    )


def generate_mir_records(claims: Iterable[Claim], client=None,
                         process_date: date | None = None) -> Tuple[List[str], Dict[str, Any]]:
    records: List[str] = []
    claim_list = list(claims)
    total_claims = 0
    total_services = 0
    split_claims = 0
    delivered_claims = 0
    delivered_services = 0
    refused_claims = 0
    warning_findings = 0
    findings: list[dict] = []
    output_bytes = 0
    fields = get_mappings(client)
    existing_icns = _persisted_icns(claim_list, client)
    seen_icns: set[str] = set()

    for claim in claim_list:
        total_claims += 1
        services = claim.services or []
        total_services += len(services)

        preventive_findings = evaluate_preventive_rules(
            claim,
            existing_icns=existing_icns,
            seen_icns=seen_icns,
        )
        icn = claim_control_number(claim)
        if icn:
            seen_icns.add(icn)

        warning_findings += sum(
            1 for finding in preventive_findings
            if str(finding.get("severity", "")).upper() == RuleSeverity.WARN.value
        )
        blocking_preventive = [finding for finding in preventive_findings if is_blocking(finding)]
        if any(
            str(finding.get("severity", "")).upper() == RuleSeverity.REFUSE.value
            for finding in blocking_preventive
        ):
            refused_claims += 1

        claim_findings = preventive_findings + _claim_findings(claim)
        if claim_findings:
            findings.extend(claim_findings)
        if any(is_blocking(finding) for finding in claim_findings):
            continue

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
        if max_sequence > config.MAX_RECORD_SEQUENCE:
            maximum_services = (
                config.MAX_SERVICE_LINES_PER_RECORD * config.MAX_RECORD_SEQUENCE
            )
            findings.append(_finding(
                claim, "RECORD_SEQUENCE_LIMIT_EXCEEDED",
                "Claim requires more MIR records than the configured sequence limit.",
                service_count=len(services),
                maximum_services=maximum_services,
                required_records=max_sequence,
                maximum_records=config.MAX_RECORD_SEQUENCE,
            ))
            continue

        inherited_reason = claim_primary_reason(claim)
        claim_records: list[str] = []
        try:
            for sequence, chunk in enumerate(chunks, start=1):
                header_service_count = len(chunk)
                record = _header(
                    claim, sequence, max_sequence, header_service_count, fields, process_date
                )
                record += "".join(
                    _service_block(
                        svc, claim, sequence, max_sequence, header_service_count,
                        inherited_reason, fields, process_date,
                    )
                    for svc in chunk
                )
                expected = config.MIR_HEADER_LENGTH + len(chunk) * config.MIR_SERVICE_BLOCK_LENGTH
                if len(record) != expected:
                    raise ValueError(
                        f"Claim {claim.claim_number} record {sequence}: expected length {expected}, got {len(record)}"
                    )
                claim_records.append(record)
        except (ValueError, ArithmeticError) as exc:
            detail = str(exc)
            code = "MIR_FIELD_OVERFLOW" if (
                "maximum is" in detail
                or "does not fit" in detail
                or "Preformatted numeric value" in detail
            ) else "MIR_GENERATION_ERROR"
            findings.append(_finding(
                claim, code,
                "Claim could not fit the MIR layout and was held.",
                detail=detail,
            ))
            continue

        claim_bytes = sum(len(record) + 2 for record in claim_records)
        if output_bytes + claim_bytes > config.MAX_OUTPUT_BYTES:
            raise ValueError(f"Generated MIR exceeds the {config.MAX_OUTPUT_BYTES} byte limit")
        records.extend(claim_records)
        output_bytes += claim_bytes
        delivered_claims += 1
        delivered_services += len(services)
        if max_sequence > 1:
            split_claims += 1

    return records, {
        "claims": total_claims,
        "services": total_services,
        "delivered_claims": delivered_claims,
        "delivered_services": delivered_services,
        "held_claims": total_claims - delivered_claims,
        "refused_claims": refused_claims,
        "warning_findings": warning_findings,
        "findings": findings,
        "mir_records": len(records),
        "split_claims": split_claims,
    }


def generate_mir_text(claims: Iterable[Claim], client=None,
                      process_date: date | None = None) -> Tuple[str, Dict[str, Any]]:
    records, summary = generate_mir_records(claims, client, process_date)
    text = "\r\n".join(records)
    if records:
        text += "\r\n"
    return text, summary
