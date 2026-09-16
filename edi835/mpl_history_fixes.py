"""Normalize duplicate MPL claim-history rows before they reach the UI.

The claim report combines two valid evidence streams:
- source_matches: the archived file occurrence
- analysis.timeline: the processing event for that same file

Those streams can encode the same timestamp differently (Z vs +00:00,
microseconds, etc.). The original tuple key compared the raw strings, so one
physical file occurrence could appear twice. This module canonicalizes the
identity and preserves the more informative timeline event.
"""

from __future__ import annotations

from datetime import datetime, timezone as pytimezone


_GENERIC_EVENTS = {
    "CLAIM FOUND IN ARCHIVED FILE",
    "CLAIM HISTORY EVENT",
}


def _file_type(item):
    value = str(item.get("file_type") or "").strip().upper()
    if value and value != "SOURCE":
        return value
    event = str(item.get("event") or "").upper()
    for candidate in ("837", "835", "RECON", "MIR"):
        if candidate in event:
            return candidate
    return value or "SOURCE"


def _canonical_date(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(pytimezone.utc)
        # UI precision is seconds. Microsecond differences between source
        # metadata and processing history must not create duplicate rows.
        return parsed.replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        return raw


def _occurrence_key(item):
    return (
        _canonical_date(item.get("date")),
        _file_type(item),
        str(item.get("filename") or "").strip().casefold(),
        str(item.get("status") or "").strip().upper(),
    )


def _prefer_event(existing, incoming):
    existing_event = str(existing.get("event") or "").strip()
    incoming_event = str(incoming.get("event") or "").strip()
    existing_generic = existing_event.upper() in _GENERIC_EVENTS or not existing_event
    incoming_generic = incoming_event.upper() in _GENERIC_EVENTS or not incoming_event
    if existing_generic and not incoming_generic:
        existing["event"] = incoming_event


def dedupe_history(history):
    """Return one row per physical file occurrence, preserving useful detail."""
    merged = {}
    order = []
    for raw in history or []:
        item = dict(raw)
        item["file_type"] = _file_type(item)

        # 837 is the inbound claim submission and does not carry the MIR/RECON
        # internal claim identifier used later in the workflow. Matching may use
        # normalized database identities internally, but that derived value must
        # not be presented as if it came from the 837 itself.
        if item["file_type"] == "837":
            item["internal_claim_number"] = ""

        key = _occurrence_key(item)
        existing = merged.get(key)
        if existing is None:
            merged[key] = item
            order.append(key)
            continue

        if (
            item["file_type"] != "837"
            and not existing.get("internal_claim_number")
            and item.get("internal_claim_number")
        ):
            existing["internal_claim_number"] = item["internal_claim_number"]
        _prefer_event(existing, item)

    return [merged[key] for key in order]


def install():
    """Wrap claim-report construction without changing matching/calculation logic."""
    from . import mpl_notices

    if getattr(mpl_notices, "_history_fixes_installed", False):
        return

    original_build = mpl_notices.build_claim_reports

    def build_claim_reports(source_matches, claims):
        reports = original_build(source_matches, claims)
        for report in reports:
            report["history"] = dedupe_history(report.get("history"))
        return reports

    mpl_notices.build_claim_reports = build_claim_reports
    mpl_notices._history_fixes_installed = True
