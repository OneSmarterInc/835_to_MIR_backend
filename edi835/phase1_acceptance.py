"""Check recorded release evidence; never invent approval or change runtime policy."""
from datetime import date


APPROVALS = {"architecture", "operating_policy", "migration_plan", "converter_baseline", "phase1_acceptance"}
EVIDENCE = {"postgresql", "capacity", "restore", "partner_compatibility", "deployed_trust", "rollback_rehearsal"}
TARGETS = {"max_queue_age_seconds", "rpo_seconds", "rto_seconds"}


def acceptance_gaps(record: dict, *, today: date | None = None) -> list[str]:
    """Validate structure plus completeness; evidence still requires human review."""
    today = today or date.today()
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ValueError("Expected acceptance record schema_version 1.")
    gaps = []
    for section, required, terminal in (("approvals", APPROVALS, "approved"),
                                        ("verification", EVIDENCE, "passed")):
        entries = record.get(section)
        if not isinstance(entries, dict) or set(entries) != required:
            raise ValueError(f"{section} must contain exactly: {', '.join(sorted(required))}.")
        for key in sorted(required):
            entry = entries[key]
            if not isinstance(entry, dict):
                raise ValueError(f"{section}.{key} must be an object.")
            if entry.get("status") not in {"pending", terminal}:
                raise ValueError(f"Invalid status for {section}.{key}.")
            if entry["status"] != terminal:
                gaps.append(f"{section}.{key}: pending")
                continue
            for field in ("owner_name", "evidence_reference"):
                value = entry.get(field)
                if not isinstance(value, str) or not value.strip():
                    gaps.append(f"{section}.{key}: {field} is required")
            try:
                recorded_date = date.fromisoformat(entry.get("recorded_on") or "")
                if recorded_date > today:
                    raise ValueError("Future approval")
            except (TypeError, ValueError):
                gaps.append(f"{section}.{key}: valid non-future recorded_on date is required")
    targets = record.get("targets")
    if not isinstance(targets, dict) or set(targets) != TARGETS:
        raise ValueError("The three queue-age/RPO/RTO targets must be present.")
    for name in sorted(TARGETS):
        value = targets[name]
        minimum = 0 if name == "rpo_seconds" else 1
        if type(value) is not int or value < minimum:
            gaps.append(f"targets.{name}: an agreed integer of at least {minimum} is required")
    return gaps
