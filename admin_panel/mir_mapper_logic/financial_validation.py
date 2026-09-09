"""Financial safety checks for MIR conversion.

These checks prevent patient-responsibility adjustments from being silently
omitted when the mapper has no safe MIR destination for them.
"""

from . import config
from .mir_mapper import normalize_text, service_status_and_reason


class UnsupportedPatientResponsibilityError(ValueError):
    """Raised when a PR adjustment cannot be represented safely in MIR."""


def validate_patient_responsibility_mapping(
    service,
    claim_status: str,
    inherited_reason: str = "",
) -> None:
    """Fail conversion when a PR adjustment has no explicit MIR destination.

    Supported patient-responsibility adjustments are either:
    - mapped to an existing MIR payment-reduction slot (PR1/PR2/PR3, PR45),
    - explicitly known paid-claim reasons, or
    - carried as the service-line denial reason.

    Anything else must fail loudly so a financial adjustment cannot disappear
    from a successfully delivered MIR.
    """
    line_status, line_reason = service_status_and_reason(
        service,
        claim_status,
        inherited_reason,
    )

    for adjustment in service.adjustments:
        if adjustment.group != config.X12_PATIENT_RESP_GROUP:
            continue

        if adjustment.reason in config.ORDINARY_PATIENT_RESPONSIBILITY_REASONS:
            continue
        if adjustment.reason == "45":
            continue

        code = normalize_text(
            f"{adjustment.group}{adjustment.reason}",
            config.PRIMARY_REASON_LENGTH,
        )

        if code in config.PAID_CLAIM_REASON_CODES:
            continue
        if line_status == "4" and line_reason == code:
            continue

        raise UnsupportedPatientResponsibilityError(
            "Unsupported patient responsibility reason "
            f"{code or '(blank)'} with amount {adjustment.amount}; "
            "MIR conversion aborted to prevent financial data loss."
        )
