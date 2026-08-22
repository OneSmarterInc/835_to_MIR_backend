"""Bounded X12 835 parser for fields needed by the MIR generator."""
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, List

from . import config
from .models import Adjustment, Claim, ServiceLine


_X12_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_X12_DATE = re.compile(r"^\d{8}$")
_ISA_SEPARATOR_OFFSETS = (3, 6, 17, 20, 31, 34, 50, 53, 69, 76, 81, 83, 89, 99, 101, 103)


def _decimal(value: str, default: str | None = None, context: str = "numeric element") -> Decimal:
    text = (value or "").strip()
    if not text:
        if default is None:
            raise ValueError(f"Missing required numeric value in {context}")
        text = default
    if not _X12_NUMBER.fullmatch(text):
        raise ValueError(f"Invalid numeric value in {context}")
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid numeric value in {context}") from exc
    if not result.is_finite():
        raise ValueError(f"Invalid numeric value in {context}")
    return result


def _date(value: str, context: str) -> str:
    value = (value or "").strip()
    if value and not _X12_DATE.fullmatch(value):
        raise ValueError(f"Invalid CCYYMMDD date in {context}")
    if value:
        try:
            datetime.strptime(value, "%Y%m%d")
        except ValueError as exc:
            raise ValueError(f"Invalid CCYYMMDD date in {context}") from exc
    return value


def _separators(text: str) -> tuple[str, str, str, str | None]:
    cleaned = text.lstrip("\ufeff\r\n\t ")
    if not cleaned.startswith("ISA"):
        return cleaned, "*", ":", "~" if "~" in cleaned else None
    if len(cleaned) < 106:
        raise ValueError("Malformed ISA segment: expected 106 characters")
    element = cleaned[3]
    component = cleaned[104]
    terminator = cleaned[105]
    if any(cleaned[offset] != element for offset in _ISA_SEPARATOR_OFFSETS):
        raise ValueError("Malformed ISA segment: invalid fixed-width separators")
    if (
        len({element, component, terminator}) != 3
        or any(ch in "\r\n" or ch.isalnum() for ch in (element, component, terminator))
    ):
        raise ValueError("Malformed ISA segment: separators must be distinct")
    return cleaned, element, component, terminator


def _segments(text: str, terminator: str | None) -> Iterable[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    raw = normalized.split(terminator) if terminator else normalized.split("\n")
    count = 0
    for seg in raw:
        seg = seg.strip()
        if seg:
            count += 1
            if count > config.MAX_X12_SEGMENTS:
                raise ValueError(f"835 exceeds the {config.MAX_X12_SEGMENTS} segment limit")
            if len(seg) > config.MAX_X12_SEGMENT_LENGTH:
                raise ValueError(f"835 segment exceeds the {config.MAX_X12_SEGMENT_LENGTH} character limit")
            yield seg


def _element(parts: List[str], number: int) -> str:
    """Return X12 element number (1-based after segment ID)."""
    return parts[number] if len(parts) > number else ""


def _parse_cas(parts: List[str]) -> List[Adjustment]:
    group = _element(parts, 1).strip().upper()
    adjustments: List[Adjustment] = []
    # CAS repetitions: reason/amount/quantity at 2-4, 5-7, ...
    idx = 2
    while idx < len(parts):
        reason = _element(parts, idx).strip().upper()
        amount_text = _element(parts, idx + 1).strip()
        quantity = _element(parts, idx + 2).strip()
        if reason:
            if not group:
                raise ValueError("Missing adjustment group in CAS01")
            if quantity:
                _decimal(quantity, context=f"CAS{idx + 2:02d}")
            adjustments.append(
                Adjustment(
                    group=group,
                    reason=reason,
                    amount=_decimal(amount_text, context=f"CAS{idx + 1:02d}"),
                    quantity=quantity,
                )
            )
        elif amount_text or quantity:
            raise ValueError(f"CAS{idx:02d} reason is required when amount or quantity is present")
        idx += 3
    return adjustments


def parse_835(text: str) -> List[Claim]:
    text, element_separator, component_separator, segment_terminator = _separators(text)
    if segment_terminator and re.search(re.escape(segment_terminator) + r"\s*ISA(?=[^A-Za-z0-9])", text):
        raise ValueError("Multiple ISA interchanges in one input are not supported")
    segments = list(_segments(text, segment_terminator))
    has_transaction_envelope = text.startswith("ISA") or any(
        segment.split(element_separator, 1)[0].strip().upper() == "ST" for segment in segments
    )
    claims: List[Claim] = []
    current: Claim | None = None
    current_service: ServiceLine | None = None
    transaction_allowed = not has_transaction_envelope
    total_services = 0

    for segment in segments:
        parts = segment.split(element_separator)
        tag = parts[0].strip().upper()

        if tag == "ST":
            current = None
            current_service = None
            transaction_allowed = _element(parts, 1).strip() == "835"
            continue
        if tag == "SE":
            current = None
            current_service = None
            transaction_allowed = False
            continue
        if not transaction_allowed:
            continue

        if tag == config.X12_SEGMENT_CLP:
            if len(claims) >= config.MAX_CLAIMS:
                raise ValueError(f"835 exceeds the {config.MAX_CLAIMS} claim limit")
            current = Claim(
                claim_number=_element(parts, 1).strip(),
                status=_element(parts, 2).strip(),
                total_charge=_decimal(_element(parts, 3), context="CLP03"),
                total_paid=_decimal(_element(parts, 4), context="CLP04"),
                patient_responsibility=_decimal(_element(parts, 5), "0", "CLP05"),
                claim_reference=_element(parts, 7).strip(),
                facility_type=_element(parts, 8).strip(),
                claim_frequency=_element(parts, 9).strip(),
            )
            claims.append(current)
            current_service = None
            continue

        if current is None:
            continue

        if tag in {"LX", "PLB"}:
            current_service = None
            if tag == "PLB":
                current = None
            continue

        if tag == config.X12_SEGMENT_NM1:
            if current_service is not None:
                continue
            entity = _element(parts, 1).upper()
            if entity == config.X12_ENTITY_PATIENT:
                current.patient_last_name = _element(parts, 3).strip()
                current.patient_first_name = _element(parts, 4).strip()
                current.patient_middle = _element(parts, 5).strip()
            elif entity == config.X12_ENTITY_SUBSCRIBER:
                # NM109 is subscriber/member ID. Prefer explicit MI qualifier, but
                # tolerate files whose optional elements vary.
                qualifier = _element(parts, 8).upper()
                if qualifier == config.X12_MEMBER_ID_QUALIFIER:
                    current.subscriber_id = _element(parts, 9).strip()

        elif tag == config.X12_SEGMENT_REF:
            if current_service is None and _element(parts, 1).upper() == config.X12_GROUP_REF_QUALIFIER:
                current.group_number = _element(parts, 2).strip()

        elif tag == config.X12_SEGMENT_DTM:
            qualifier = _element(parts, 1)
            value = _element(parts, 2).strip()
            if qualifier == config.X12_DOB_QUALIFIER and current_service is None:
                current.dob = _date(value, "DTM02 with qualifier 036")
            elif qualifier == config.X12_CLAIM_RECEIVED_DATE_QUALIFIER and current_service is None:
                current.claim_received_date = _date(value, "DTM02 with qualifier 050")
            elif qualifier == config.X12_SERVICE_DATE_QUALIFIER and current_service is not None:
                current_service.service_date = _date(value, "DTM02 with qualifier 472")

        elif tag == config.X12_SEGMENT_SVC:
            if total_services >= config.MAX_SERVICE_LINES:
                raise ValueError(f"835 exceeds the {config.MAX_SERVICE_LINES} service-line limit")
            composite = _element(parts, 1).strip()
            procedure = composite.split(component_separator, 1)[1] if component_separator in composite else composite
            units = _decimal(_element(parts, 5), "1", "SVC05")
            current_service = ServiceLine(
                procedure=procedure,
                charge=_decimal(_element(parts, 2), context="SVC02"),
                paid=_decimal(_element(parts, 3), context="SVC03"),
                units=units,
            )
            current.services.append(current_service)
            total_services += 1

        elif tag == config.X12_SEGMENT_CAS:
            adjustments = _parse_cas(parts)
            if current_service is None:
                current.adjustments.extend(adjustments)
            else:
                current_service.adjustments.extend(adjustments)

    return claims
