"""Business calculations used by the runtime MIR mapping engine."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, Tuple

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
    return total


def covered_charge(service: ServiceLine) -> Decimal:
    return service.charge - co_adjustment_total(service)


def patient_liability(service: ServiceLine) -> Decimal:
    return covered_charge(service) - service.paid


def first_adjustment_code(service: ServiceLine, group: str | None = None) -> str:
    for adj in service.adjustments:
        if group is not None and adj.group != group:
            continue
        candidate = f"{adj.group}{adj.reason}"
        if candidate:
            return normalize_text(candidate, config.PRIMARY_REASON_LENGTH)
    return ""


def _adjustment_priority(group: str) -> int:
    return {"PR": 0, "PI": 1, "OA": 2, "CO": 3}.get(group, 9)


def denial_adjustment(service: ServiceLine, require_full_reduction: bool = False):
    candidates = []
    has_ordinary_patient_reduction = any(
        adjustment.group == config.X12_PATIENT_RESP_GROUP
        and adjustment.reason in config.ORDINARY_PATIENT_RESPONSIBILITY_REASONS
        for adjustment in service.adjustments
    )
    for order, adjustment in enumerate(service.adjustments):
        code = normalize_text(f"{adjustment.group}{adjustment.reason}", config.PRIMARY_REASON_LENGTH)
        if not code or code in config.PAID_CLAIM_REASON_CODES:
            continue
        if adjustment.group == "CO" and adjustment.reason == config.STANDARD_CONTRACTUAL_PRICING_REASON:
            continue
        if (
            adjustment.group == config.X12_PATIENT_RESP_GROUP
            and adjustment.reason in config.ORDINARY_PATIENT_RESPONSIBILITY_REASONS
        ):
            continue
        if adjustment.group == "PI" and has_ordinary_patient_reduction:
            continue
        patient_denial = adjustment.group == "PR"
        if (
            require_full_reduction
            and not patient_denial
            and service.charge > 0
            and adjustment.amount < service.charge
        ):
            continue
        candidates.append((_adjustment_priority(adjustment.group), order, adjustment))
    return min(candidates, default=(None, None, None))[2]


def has_only_routine_pricing_adjustments(service: ServiceLine) -> bool:
    """Return True when a line contains cost-share/pricing edits, not a denial."""
    if not service.adjustments:
        return False
    return all(
        (
            adjustment.group == config.X12_CONTRACTUAL_GROUP
            and adjustment.reason == config.STANDARD_CONTRACTUAL_PRICING_REASON
        )
        or (
            adjustment.group == config.X12_PATIENT_RESP_GROUP
            and adjustment.reason in config.ORDINARY_PATIENT_RESPONSIBILITY_REASONS
        )
        for adjustment in service.adjustments
    )


def claim_has_only_routine_pricing_adjustments(claim: Claim) -> bool:
    """A non-paid CLP can still be a paid MIR when CAS contains routine edits only."""
    return bool(claim.services) and all(
        has_only_routine_pricing_adjustments(service) for service in claim.services
    )


def claim_disposition(claim: Claim) -> str:
    """Derive MIR202 instead of copying CLP02 directly."""
    if claim.status != config.PAID_CLAIM_STATUS:
        return "1" if claim_has_only_routine_pricing_adjustments(claim) else "4"
    for service in claim.services:
        adjustment = denial_adjustment(service, require_full_reduction=True)
        if adjustment and adjustment.reason == "B11":
            return "4"
    return "1"


def process_code_indicator(claim: Claim) -> str:
    """Derive MIR501 from streamline and adjustment indicators in CLP."""
    if claim.facility_type == config.STREAMLINE_FACILITY_TYPE:
        return "A"
    if claim.claim_frequency in config.ADJUSTMENT_FREQUENCY_CODES:
        return "A"
    return ""


def claim_primary_reason(claim: Claim) -> str:
    # Non-paid claim dispositions in the supplied pair use the first CAS reason.
    if claim_disposition(claim) == "4":
        for service in claim.services:
            adjustment = denial_adjustment(
                service,
                require_full_reduction=claim.status == config.PAID_CLAIM_STATUS,
            )
            if adjustment:
                return normalize_text(f"{adjustment.group}{adjustment.reason}", config.PRIMARY_REASON_LENGTH)
        for adjustment in claim.adjustments:
            code = normalize_text(f"{adjustment.group}{adjustment.reason}", config.PRIMARY_REASON_LENGTH)
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
        if has_only_routine_pricing_adjustments(service):
            # A routine CO45/PR1-3 line is paid only when the whole claim was
            # classified as routine (there is no inherited denial reason).
            # Inside a genuinely denied claim it inherits the claim denial.
            return ("4", inherited_reason) if inherited_reason else ("1", "")
        adjustment = denial_adjustment(service)
        reason = (
            normalize_text(f"{adjustment.group}{adjustment.reason}", config.PRIMARY_REASON_LENGTH)
            if adjustment else first_adjustment_code(service) or inherited_reason
        )
        return "4", reason

    # Claim-level edit codes such as CO41 are carried on each line while the
    # claim remains paid status 1.
    if inherited_reason in config.PAID_CLAIM_REASON_CODES:
        return claim_status, inherited_reason

    # A line inside an otherwise paid claim can be denied/patient-responsibility
    # only.  The reference MIR marks those line items as status 4 with the PR code.
    adjustment = denial_adjustment(service, require_full_reduction=True)
    if adjustment and (adjustment.reason == "B11" or service.paid == 0):
        return "4", normalize_text(f"{adjustment.group}{adjustment.reason}", config.PRIMARY_REASON_LENGTH)

    return claim_status, ""


def prepriced_amount(service: ServiceLine, claim_status: str, inherited_reason: str = "") -> Decimal:
    if has_only_routine_pricing_adjustments(service):
        return covered_charge(service)
    status, reason = service_status_and_reason(service, claim_status, inherited_reason)
    if status == "4" and (
        (claim_status != config.PAID_CLAIM_STATUS and reason != "CON89")
        or reason.endswith("B11")
    ):
        return service.charge
    return covered_charge(service)


def mir_patient_liability(service: ServiceLine, claim_status: str, inherited_reason: str = "") -> Decimal:
    if has_only_routine_pricing_adjustments(service):
        return patient_liability(service)
    status, reason = service_status_and_reason(service, claim_status, inherited_reason)
    if status == "4" and reason.startswith("PR"):
        patient_amount = sum(
            (adjustment.amount for adjustment in service.adjustments if adjustment.group == "PR"),
            Decimal("0"),
        )
        if patient_amount:
            return min(patient_amount, service.charge) if service.charge > 0 else patient_amount
    if status == "4" and (
        (claim_status != config.PAID_CLAIM_STATUS and reason != "CON89")
        or reason.endswith("B11")
    ):
        return service.charge
    return patient_liability(service)


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


def payment_reduction_slots(service: ServiceLine) -> Dict[int, Tuple[str, Decimal]]:
    """Return MIR slot -> (combined group/reason, amount)."""
    result: Dict[int, Tuple[str, Decimal]] = {}
    for adjustment in service.adjustments:
        if adjustment.group != config.X12_PATIENT_RESP_GROUP:
            continue
        if adjustment.reason in config.ORDINARY_PATIENT_RESPONSIBILITY_REASONS:
            slot = int(adjustment.reason)
            code = f"PR{adjustment.reason}"
        elif adjustment.reason == "45":
            slot = 3
            code = "PR119"
        else:
            continue
        existing = result.get(slot)
        amount = adjustment.amount + (existing[1] if existing else Decimal("0"))
        result[slot] = (code, amount)
    return result
