"""Business calculations used by the runtime MIR mapping engine."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict

from . import config
from .models import Claim, ServiceLine


def normalize_text(value: str, length: int) -> str:
    return (value or "").strip().upper()[:length]


def signed_amount(value: Decimal, digits: int = config.SIGNED_AMOUNT_DIGITS) -> str:
    q = Decimal("1").scaleb(-config.AMOUNT_DECIMAL_PLACES)
    try:
        value = value.quantize(q, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("Amount must be a finite decimal") from exc
    if not value.is_finite():
        raise ValueError("Amount must be a finite decimal")
    sign = "+" if value >= 0 else "-"
    cents = int(abs(value) * (10 ** config.AMOUNT_DECIMAL_PLACES))
    if cents >= 10 ** digits:
        raise ValueError(f"Amount does not fit in {digits} MIR digits")
    return f"{cents:0{digits}d}{sign}"


def signed_count(value: Decimal, digits: int) -> str:
    try:
        rounded_value = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("Count must be a finite decimal") from exc
    if not rounded_value.is_finite():
        raise ValueError("Count must be a finite decimal")
    rounded = int(rounded_value)
    if abs(rounded) >= 10 ** digits:
        raise ValueError(f"Count does not fit in {digits} MIR digits")
    sign = "+" if rounded >= 0 else "-"
    return f"{abs(rounded):0{digits}d}{sign}"


def co_adjustment_total(service: ServiceLine) -> Decimal:
    total = sum((a.amount for a in service.adjustments if a.group == config.X12_CONTRACTUAL_GROUP), Decimal("0"))
    # Some supplied CAS segments repeat a positive adjustment.  The real MIR
    # never lets a positive contractual reduction drive covered charge below
    # zero, so cap only the positive side.  Negative CO adjustments are valid
    # reversals/increases and must remain negative.
    if total > service.charge:
        total = service.charge
    return total


def covered_charge(service: ServiceLine) -> Decimal:
    value = service.charge - co_adjustment_total(service)
    if not config.ALLOW_NEGATIVE_DERIVED_AMOUNTS:
        value = max(value, Decimal("0"))
    return value


def patient_liability(service: ServiceLine) -> Decimal:
    value = covered_charge(service) - service.paid
    if not config.ALLOW_NEGATIVE_DERIVED_AMOUNTS:
        value = max(value, Decimal("0"))
    return value


def first_adjustment_code(service: ServiceLine, group: str | None = None) -> str:
    for adj in service.adjustments:
        if group is not None and adj.group != group:
            continue
        candidate = f"{adj.group}{adj.reason}"
        if candidate:
            return normalize_text(candidate, config.PRIMARY_REASON_LENGTH)
    return ""


def claim_primary_reason(claim: Claim) -> str:
    # Non-paid claim dispositions in the supplied pair use the first CAS reason.
    if claim.status != config.PAID_CLAIM_STATUS:
        for adjustment in claim.adjustments:
            code = normalize_text(f"{adjustment.group}{adjustment.reason}", config.PRIMARY_REASON_LENGTH)
            if code:
                return code
        for service in claim.services:
            code = first_adjustment_code(service)
            if code:
                return code
        return ""

    # A paid claim can still carry a claim-level CO edit when one or more lines
    # are fully reduced by a non-standard contractual reason (e.g. CO41 in the
    # supplied reference).  CO45 is ordinary contractual pricing and is not
    # promoted to the claim header.
    for adjustment in claim.adjustments:
        if (
            adjustment.group == config.X12_CONTRACTUAL_GROUP
            and adjustment.reason != config.STANDARD_CONTRACTUAL_PRICING_REASON
            and adjustment.amount >= claim.total_charge
            and claim.total_charge > 0
        ):
            return normalize_text(f"{adjustment.group}{adjustment.reason}", config.PRIMARY_REASON_LENGTH)
    for service in claim.services:
        for adj in service.adjustments:
            if adj.group == config.X12_CONTRACTUAL_GROUP and adj.reason != config.STANDARD_CONTRACTUAL_PRICING_REASON and adj.amount >= service.charge and service.charge > 0:
                return normalize_text(f"{adj.group}{adj.reason}", config.PRIMARY_REASON_LENGTH)
    return ""


def service_status_and_reason(service: ServiceLine, claim_status: str, inherited_reason: str = "") -> tuple[str, str]:
    if claim_status != config.PAID_CLAIM_STATUS:
        return claim_status, first_adjustment_code(service) or inherited_reason

    # Claim-level edit codes such as CO41 are carried on each line while the
    # claim remains paid status 1.
    if inherited_reason:
        return claim_status, inherited_reason

    # A line inside an otherwise paid claim can be denied/patient-responsibility
    # only.  The reference MIR marks those line items as status 4 with the PR code.
    if service.paid == 0 and patient_liability(service) > 0:
        for adj in service.adjustments:
            if adj.group != config.X12_PATIENT_RESP_GROUP:
                continue
            # PR1/PR2/PR3 are ordinary deductible/coinsurance/copay and do not
            # turn an otherwise paid claim line into MIR status 4.  Other PR
            # reasons observed in the supplied pair (e.g. PR31/PR119) do.
            if adj.reason not in config.ORDINARY_PATIENT_RESPONSIBILITY_REASONS:
                return "4", normalize_text(f"{config.X12_PATIENT_RESP_GROUP}{adj.reason}", config.PRIMARY_REASON_LENGTH)

    return claim_status, ""


def payment_reductions(service: ServiceLine) -> Dict[int, Decimal]:
    result: Dict[int, Decimal] = {}
    for adj in service.adjustments:
        if adj.group != config.PAYMENT_REDUCTION_CODE_PREFIX:
            continue
        try:
            reason_number = int(adj.reason)
        except ValueError:
            continue
        if config.PAYMENT_REDUCTION_MIN_REASON <= reason_number <= config.PAYMENT_REDUCTION_MAX_REASON:
            result[reason_number] = result.get(reason_number, Decimal("0")) + adj.amount
    return result
