"""Preventive MIR gate framework with auditable rule provenance.

The registry intentionally separates rule metadata (code/source/severity) from
rule evaluation so MPL-derived checks are discoverable, consistently reported,
and can be extended without scattering policy through the generator.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Callable, Iterable

from .mir_mapper import (
    mir_patient_liability,
    prepriced_amount,
    service_status_and_reason,
)
from .models import Claim


MPL_SOURCE = "MPL_Exception-Codes-List-5_2-v21_0.docx"


class RuleSeverity(str, Enum):
    REFUSE = "REFUSE"
    HOLD = "HOLD"
    WARN = "WARN"


@dataclass(frozen=True)
class RuleDefinition:
    code: str
    name: str
    source: str
    severity: RuleSeverity
    scope: str
    description: str
    evaluator: Callable[[Claim, "RuleContext"], list[dict]]


@dataclass(frozen=True)
class RuleContext:
    existing_icns: frozenset[str] = frozenset()
    seen_icns: frozenset[str] = frozenset()


class RuleRegistry:
    def __init__(self) -> None:
        self._rules: dict[str, RuleDefinition] = {}

    def register(self, rule: RuleDefinition) -> None:
        if rule.code in self._rules:
            raise ValueError(f"Duplicate rule code {rule.code}")
        self._rules[rule.code] = rule

    def definitions(self) -> tuple[RuleDefinition, ...]:
        return tuple(self._rules.values())

    def evaluate(self, claim: Claim, context: RuleContext | None = None) -> list[dict]:
        ctx = context or RuleContext()
        findings: list[dict] = []
        for rule in self._rules.values():
            for raw in rule.evaluator(claim, ctx):
                finding = {
                    "rule_code": rule.code,
                    "rule_name": rule.name,
                    "source": rule.source,
                    "severity": rule.severity.value,
                    "scope": rule.scope,
                    "claim_number": claim.claim_number,
                    "claim_control_number": claim_control_number(claim),
                    "reason": raw.get("reason") or rule.description,
                    "provenance": {
                        "rule_code": rule.code,
                        "source": rule.source,
                        "description": rule.description,
                    },
                }
                finding.update({key: value for key, value in raw.items() if key != "reason"})
                findings.append(finding)
        return findings


def claim_control_number(claim: Claim) -> str:
    """Return the full MIR claim control number (CLP01 + CLP07 suffix)."""
    return f"{claim.claim_number or ''}{claim.claim_reference or ''}".strip()


def _mp003_cross_foot(claim: Claim, _context: RuleContext) -> list[dict]:
    """MIR1017 must equal MIR1018 + MIR1019 for every service line."""
    findings: list[dict] = []
    inherited_reason = ""
    for index, service in enumerate(claim.services or [], start=1):
        allowance = prepriced_amount(service, claim.status, inherited_reason)
        approved_to_pay = service.paid
        liability = mir_patient_liability(service, claim.status, inherited_reason)
        expected = approved_to_pay + liability
        if allowance != expected:
            findings.append({
                "service_line": index,
                "reason": "BCBS allowance does not equal approved-to-pay plus patient liability.",
                "evidence": {
                    "mir1017_allowance": str(allowance),
                    "mir1018_approved_to_pay": str(approved_to_pay),
                    "mir1019_patient_liability": str(liability),
                    "approved_plus_liability": str(expected),
                },
            })
    return findings


def _is_timely_filing_claim(claim: Claim) -> bool:
    # CARC 29: time limit for filing has expired.  MPL MP011 applies to the
    # resulting Timely Filing claim rather than inventing a local day threshold.
    adjustments = list(claim.adjustments or [])
    for service in claim.services or []:
        adjustments.extend(service.adjustments or [])
    return any(str(adjustment.reason).strip().upper() == "29" for adjustment in adjustments)


def _mp011_timely_filing(claim: Claim, _context: RuleContext) -> list[dict]:
    if not _is_timely_filing_claim(claim):
        return []

    inherited_reason = ""
    violations: list[dict] = []
    for index, service in enumerate(claim.services or [], start=1):
        line_status, _ = service_status_and_reason(service, claim.status, inherited_reason)
        liability = mir_patient_liability(service, claim.status, inherited_reason)
        if line_status != "4" or service.paid != Decimal("0") or liability != Decimal("0"):
            violations.append({
                "service_line": index,
                "line_status": line_status,
                "fund_amount": str(service.paid),
                "patient_liability": str(liability),
            })

    if not violations:
        return []
    return [{
        "reason": "Timely Filing claim must have all lines denied with zero fund and patient-liability amounts.",
        "evidence": {"violating_lines": violations},
    }]


def _has_pr31(claim: Claim) -> bool:
    adjustments = list(claim.adjustments or [])
    for service in claim.services or []:
        adjustments.extend(service.adjustments or [])
    return any(
        str(adjustment.group).strip().upper() == "PR"
        and str(adjustment.reason).strip().upper() == "31"
        for adjustment in adjustments
    )


def _mp013_group_number(claim: Claim, _context: RuleContext) -> list[dict]:
    # The current 835 parser exposes REF*1L as Claim.group_number but does not
    # expose a reliable Highmark plan-code field. Prevent the known downstream
    # rejection when the subgroup/group number is absent, while retaining the
    # documented PR31 exception.
    if (claim.group_number or "").strip() or _has_pr31(claim):
        return []
    return [{
        "reason": "Group/Sub-Group Number is required; REF*1L/REF02 is blank.",
        "evidence": {"group_number": "", "pr31_exception": False},
    }]


def _duplicate_icn(claim: Claim, context: RuleContext) -> list[dict]:
    icn = claim_control_number(claim)
    if not icn:
        return []
    sources: list[str] = []
    if icn in context.existing_icns:
        sources.append("persisted MIR history")
    if icn in context.seen_icns:
        sources.append("current batch")
    if not sources:
        return []
    return [{
        "reason": "Duplicate full claim control number (ICN) detected.",
        "evidence": {"icn": icn, "duplicate_sources": sources},
    }]


RULE_REGISTRY = RuleRegistry()
RULE_REGISTRY.register(RuleDefinition(
    code="MP003",
    name="Claim cross-foot",
    source=MPL_SOURCE,
    severity=RuleSeverity.REFUSE,
    scope="service",
    description=(
        "BCBS Allowance (MIR1017) must equal Approved to Pay (MIR1018) "
        "+ Patient Liability (MIR1019)."
    ),
    evaluator=_mp003_cross_foot,
))
RULE_REGISTRY.register(RuleDefinition(
    code="MP011",
    name="Timely filing",
    source=MPL_SOURCE,
    severity=RuleSeverity.REFUSE,
    scope="claim",
    description=(
        "A Timely Filing claim must have all lines denied and Fund/Patient "
        "Liability amounts equal to zero."
    ),
    evaluator=_mp011_timely_filing,
))
RULE_REGISTRY.register(RuleDefinition(
    code="MP013",
    name="Group/Sub-Group Number",
    source=MPL_SOURCE,
    severity=RuleSeverity.REFUSE,
    scope="claim",
    description="Required MIR group/sub-group number must be populated unless the PR31 exception applies.",
    evaluator=_mp013_group_number,
))
RULE_REGISTRY.register(RuleDefinition(
    code="DUPLICATE_ICN",
    name="Duplicate ICN",
    source="OneSmarter preventive intake control",
    # Deterministic regeneration of a previously processed source must remain
    # possible. Keep duplicate detection visible without holding the claim.
    severity=RuleSeverity.WARN,
    scope="claim",
    description="The full claim control number (CLP01 + CLP07) must not already have been processed or repeated in the batch.",
    evaluator=_duplicate_icn,
))


def evaluate_preventive_rules(
    claim: Claim,
    *,
    existing_icns: Iterable[str] = (),
    seen_icns: Iterable[str] = (),
) -> list[dict]:
    return RULE_REGISTRY.evaluate(
        claim,
        RuleContext(frozenset(existing_icns), frozenset(seen_icns)),
    )


def is_blocking(finding: dict) -> bool:
    return str(finding.get("severity", "")).upper() in {
        RuleSeverity.REFUSE.value,
        RuleSeverity.HOLD.value,
    }
