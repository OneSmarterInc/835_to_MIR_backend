"""Preventive MIR-to-837 consistency checks.

B2 is deliberately limited to source facts that should remain stable across
837 submission, 835 adjudication, and MIR generation: service-line count and
submitted charge. Adjudication values such as paid amount, patient liability,
CAS adjustments, and disposition are intentionally not compared to the 837.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.db.models import Q

from admin_panel.mir_mapper_logic import config
from admin_panel.mir_mapper_logic.edi835_parser import parse_835

from .models import EDI837Claim


class MIR837ConsistencyError(ValueError):
    """Raised when a comparable generated MIR materially disagrees with 837."""


def _money(value) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


def _signed_implied_decimal(value: str) -> Decimal:
    text = (value or "").strip()
    if not text:
        return Decimal("0.00")
    sign = Decimal("-1") if text[-1:] == "-" else Decimal("1")
    digits = text[:-1] if text[-1:] in "+-" else text
    try:
        return (sign * Decimal(digits or "0").scaleb(-2)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid MIR signed amount {value!r}")


def _mir_claim_summaries(mir_text: str) -> list[dict]:
    """Read logical claim service counts and submitted charges from MIR text."""
    summaries = []
    current = None

    for row_number, row in enumerate((mir_text or "").splitlines(), start=1):
        if not row.strip():
            continue
        if len(row) < config.MIR_HEADER_LENGTH:
            raise ValueError(
                f"B2 cannot inspect MIR row {row_number}: row is shorter than the MIR header"
            )
        service_area = len(row) - config.MIR_HEADER_LENGTH
        if service_area % config.MIR_SERVICE_BLOCK_LENGTH:
            raise ValueError(
                f"B2 cannot inspect MIR row {row_number}: invalid service-block length"
            )
        actual_count = service_area // config.MIR_SERVICE_BLOCK_LENGTH
        try:
            declared_count = int((row[332:334] or "0").strip() or "0")
            sequence = int((row[248:250] or "1").strip() or "1")
        except ValueError as exc:
            raise ValueError(f"B2 cannot inspect MIR row {row_number}: invalid count/sequence") from exc
        if actual_count != declared_count:
            raise ValueError(
                f"B2 cannot inspect MIR row {row_number}: declares {declared_count} services but contains {actual_count}"
            )

        if sequence == 1:
            current = {
                "claim_number": row[2:19].strip(),
                "service_count": 0,
                "charge_total": Decimal("0.00"),
            }
            summaries.append(current)
        elif current is None:
            raise ValueError(f"B2 cannot inspect MIR row {row_number}: orphan continuation record")

        for position in range(actual_count):
            start = config.MIR_HEADER_LENGTH + position * config.MIR_SERVICE_BLOCK_LENGTH
            raw_service = row[start:start + config.MIR_SERVICE_BLOCK_LENGTH]
            current["service_count"] += 1
            current["charge_total"] += _signed_implied_decimal(raw_service[50:61])

    for summary in summaries:
        summary["charge_total"] = _money(summary["charge_total"])
    return summaries


def _candidate_837_claims(client, claim):
    claim_number = str(getattr(claim, "claim_number", "") or "").strip()
    claim_reference = str(getattr(claim, "claim_reference", "") or "").strip()
    if not claim_number and not claim_reference:
        return []

    lookup = Q()
    if claim_number:
        lookup |= Q(claim_control_number=claim_number)
        lookup |= Q(patient_control_number=claim_number)
        lookup |= Q(highmark_claim_number=claim_number)
        lookup |= Q(internal_claim_number=claim_number)
        lookup |= Q(reference_9c=claim_number)
    if claim_reference:
        lookup |= Q(reference_9c=claim_reference)
        lookup |= Q(internal_claim_number=claim_reference)
        lookup |= Q(claim_control_number=claim_reference)

    return list(
        EDI837Claim.objects.filter(
            client=client,
            edi_file__status="PROCESSED",
        )
        .filter(lookup)
        .prefetch_related("service_lines")
        .order_by("-edi_file__uploaded_at", "claim_sequence")
    )


def _837_signature(candidate) -> tuple[int, Decimal, tuple[Decimal, ...]]:
    service_charges = tuple(
        _money(value)
        for value in candidate.service_lines.order_by("service_sequence").values_list("charge_amount", flat=True)
    )
    return (
        int(candidate.service_count or 0),
        _money(candidate.total_charge_amount),
        service_charges,
    )


def _resolve_837_candidate(client, claim):
    """Return one unambiguous logical 837 match, tolerating duplicate intakes."""
    candidates = _candidate_837_claims(client, claim)
    if not candidates:
        return None, "NO_MATCH"

    signatures = {}
    for candidate in candidates:
        signatures.setdefault(_837_signature(candidate), candidate)
    if len(signatures) != 1:
        return None, "AMBIGUOUS"
    return next(iter(signatures.values())), "MATCH"


def validate_generated_mir_against_837(
    *,
    client,
    mir_text: str,
    edi_text: str | None = None,
    claims=None,
) -> dict:
    """Validate generated MIR against a confidently matched stored 837.

    Backwards-compatibility rule: B2 blocks only when the stored 837 and source
    835 already agree on the stable facts being checked. If no 837 exists, the
    match is ambiguous, or the payer's 835 itself differs from the 837, B2 does
    not guess and does not turn an existing valid conversion into a new failure.
    """
    if client is None:
        return {"checked": 0, "skipped": 0, "warnings": [], "blocking": []}

    source_claims = list(claims if claims is not None else parse_835(edi_text or ""))
    mir_claims = _mir_claim_summaries(mir_text)
    warnings = []
    blocking = []
    checked = 0
    skipped = 0

    for index, source_claim in enumerate(source_claims):
        candidate, match_state = _resolve_837_candidate(client, source_claim)
        claim_id = str(getattr(source_claim, "claim_number", "") or f"claim-{index + 1}")

        if candidate is None:
            skipped += 1
            if match_state == "AMBIGUOUS":
                warnings.append(
                    f"B2 skipped claim {claim_id}: multiple conflicting stored 837 matches exist."
                )
            continue

        source_services = list(getattr(source_claim, "services", None) or [])
        source_service_total = _money(sum((_money(service.charge) for service in source_services), Decimal("0.00")))
        source_claim_total = _money(getattr(source_claim, "total_charge", 0))
        expected_count = int(candidate.service_count or 0)
        expected_total = _money(candidate.total_charge_amount)
        stored_837_service_total = _money(
            sum(
                (_money(value) for value in candidate.service_lines.values_list("charge_amount", flat=True)),
                Decimal("0.00"),
            )
        )

        comparable = (
            expected_count == len(source_services)
            and expected_total == source_claim_total
            and expected_total == source_service_total
            and expected_total == stored_837_service_total
        )
        if not comparable:
            skipped += 1
            warnings.append(
                f"B2 skipped claim {claim_id}: stored 837 and source 835 do not agree on service count/charge, so MIR was not judged against a mismatched source."
            )
            continue

        checked += 1
        if index >= len(mir_claims):
            blocking.append(
                f"claim {claim_id}: 837 has {expected_count} service lines / {expected_total:.2f} charge, but generated MIR claim is missing"
            )
            continue

        generated = mir_claims[index]
        if generated["service_count"] != expected_count or generated["charge_total"] != expected_total:
            blocking.append(
                f"claim {claim_id}: 837 has {expected_count} service lines / {expected_total:.2f} charge, generated MIR has {generated['service_count']} / {generated['charge_total']:.2f}"
            )

    if len(mir_claims) < len(source_claims):
        # Comparable missing claims are already reported above. Do not create a
        # generic failure for unmatched/non-comparable source claims.
        pass

    if blocking:
        raise MIR837ConsistencyError(
            "B2 MIR-to-837 consistency check failed: "
            + "; ".join(blocking)
            + "; MIR delivery aborted."
        )

    return {
        "checked": checked,
        "skipped": skipped,
        "warnings": warnings,
        "blocking": [],
    }
