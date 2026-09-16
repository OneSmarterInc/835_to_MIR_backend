"""Runtime normalization for MPL source identities and claim presentation.

This keeps source matching tied to database-backed files while correcting legacy
fixed-width values that accidentally include data following the six-character
internal claim number. Detail serialization also exposes the stored AI rewrite
directly through claim reports so the React UI does not need a DOM injector.
"""

import json
import re
from uuid import UUID

from django.db.models import Q

from .models import EDI835Claim, EDI837Claim, MIRClaim, RECONClaim


_INTERNAL_SIX = re.compile(r"^[A-Z]{3}\d{3}$", re.I)
_INTERNAL_PREFIX = re.compile(r"^([A-Z]{3}\d{3})(?=\d{4,})", re.I)


def strict_internal_claim_number_from_source(highmark_claim_number, *database_values):
    wanted = str(highmark_claim_number or "").strip().upper()
    if not wanted:
        return ""

    normalized = [str(value or "").strip() for value in database_values if str(value or "").strip()]

    packed = re.compile(
        rf"(?:HI)?{re.escape(wanted)}[^A-Z0-9]*([A-Z]{{3}}\d{{3}})",
        re.I,
    )
    for value in normalized:
        match = packed.search(value)
        if match:
            return match.group(1).upper()

    for value in normalized:
        candidate = value.strip()
        if _INTERNAL_SIX.fullmatch(candidate):
            return candidate.upper()
        prefix = _INTERNAL_PREFIX.match(candidate)
        if prefix:
            return prefix.group(1).upper()

    for value in normalized:
        if (
            value.upper() != wanted
            and len(value) <= 20
            and re.search(r"[A-Za-z]", value)
            and re.search(r"\d", value)
            and re.fullmatch(r"[A-Za-z0-9_-]+", value)
        ):
            return value
    return ""


def _file_id_from_source(source):
    url = str(source.get("download_url") or "")
    match = re.search(r"/mpl-files/[^/]+/([0-9a-f-]{36})/download/", url, re.I)
    if not match:
        return None
    try:
        return UUID(match.group(1))
    except ValueError:
        return None


def _actual_occurrence_count(source, claim_number, internal_number):
    source_type = str(source.get("type") or "").upper()
    file_id = _file_id_from_source(source)
    if not file_id:
        return 1

    claim_number = str(claim_number or "").strip()
    internal_number = str(internal_number or "").strip()

    if source_type == "835":
        query = EDI835Claim.objects.filter(edi_file_id=file_id)
        if claim_number:
            query = query.filter(highmark_claim_number__iexact=claim_number)
        if internal_number:
            query = query.filter(internal_claim_number__iexact=internal_number)
        return max(1, query.count())

    if source_type == "837":
        query = EDI837Claim.objects.filter(edi_file_id=file_id)
        if claim_number:
            query = query.filter(
                Q(highmark_claim_number__iexact=claim_number)
                | Q(claim_control_number__iexact=claim_number)
                | Q(raw_claim__contains=claim_number)
            )
        if internal_number:
            query = query.filter(
                Q(internal_claim_number__iexact=internal_number)
                | Q(reference_9c__iexact=internal_number)
                | Q(raw_claim__contains=internal_number)
            )
        return max(1, query.count())

    if source_type == "MIR":
        query = MIRClaim.objects.filter(mir_file_id=file_id)
        if claim_number:
            query = query.filter(
                Q(claim_control_number__istartswith=claim_number)
                | Q(header_raw__contains=claim_number)
            )
        if internal_number:
            query = query.filter(
                Q(claim_control_number__icontains=internal_number)
                | Q(header_raw__contains=internal_number)
            )
        return max(1, query.count())

    if source_type == "RECON":
        query = RECONClaim.objects.filter(recon_file_id=file_id)
        if claim_number:
            query = query.filter(
                Q(claim_control_number__istartswith=claim_number)
                | Q(patient_control_number__istartswith=claim_number)
                | Q(raw_record__contains=claim_number)
            )
        if internal_number:
            query = query.filter(
                Q(claim_control_number__icontains=internal_number)
                | Q(patient_control_number__icontains=internal_number)
                | Q(raw_record__contains=internal_number)
            )
        return max(1, query.count())

    return 1


def _normalize_and_dedupe_matches(matches):
    cleaned = []
    for match in matches or []:
        claim_number = str(match.get("claim_number") or "").strip()
        item = dict(match)
        output_sources = []
        seen_counts = {}
        allowed_counts = {}

        for raw_source in match.get("sources") or []:
            source = dict(raw_source)
            internal = strict_internal_claim_number_from_source(
                claim_number,
                source.get("internal_claim_number"),
            )
            source["internal_claim_number"] = internal
            file_id = _file_id_from_source(source)
            identity = (
                str(source.get("type") or "").upper(),
                str(file_id or source.get("download_url") or source.get("filename") or ""),
                internal.upper(),
            )
            seen_counts[identity] = seen_counts.get(identity, 0) + 1
            if identity not in allowed_counts:
                allowed_counts[identity] = _actual_occurrence_count(source, claim_number, internal)
            if seen_counts[identity] <= allowed_counts[identity]:
                output_sources.append(source)

        item["sources"] = output_sources
        cleaned.append(item)
    return cleaned


def _ai_claim_map(notice):
    """Return stored AI rewrites keyed by the displayed claim number."""
    raw = str(getattr(notice, "ai_response", "") or "").strip()
    source = str(getattr(notice, "ai_response_source", "") or "").strip()
    if not raw or not source or source in {"python-rules-v1", "deterministic-fallback"}:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    output = {}
    for item in payload.get("claims") or []:
        if not isinstance(item, dict):
            continue
        claim_number = str(item.get("claim_number") or "").strip()
        paragraph = str(item.get("paragraph") or "").strip()
        bullets = [
            str(value or "").strip()
            for value in (item.get("bullets") or [])
            if str(value or "").strip()
        ]
        if claim_number and (paragraph or bullets):
            output[claim_number.upper()] = {
                "paragraph": paragraph,
                "bullets": bullets,
                "source": source,
            }
    return output


def _apply_ai_claim_reports(data, notice):
    """Make React claim cards consume AI text without client-side DOM mutation."""
    reports = data.get("claim_reports") or []
    if not reports:
        return
    ai_map = _ai_claim_map(notice)
    analysis_finished = str(getattr(notice, "status", "") or "").upper() in {
        "COMPLETED", "REVIEW_REQUIRED", "FAILED"
    }
    for report in reports:
        claim_number = str(report.get("claim_number") or "").strip()
        suggestion = ai_map.get(claim_number.upper())
        report["ai_suggestion"] = suggestion
        report["ai_suggestion_state"] = "READY" if suggestion else (
            "UNAVAILABLE" if analysis_finished else "PROCESSING"
        )
        if suggestion:
            display = []
            if suggestion["paragraph"]:
                display.append({"text": suggestion["paragraph"], "kind": "paragraph"})
            display.extend(
                {"text": bullet, "kind": "bullet"}
                for bullet in suggestion["bullets"]
            )
            report["recommended_actions"] = display
        else:
            report["recommended_actions"] = [{
                "text": (
                    "AI suggestion is under analysis."
                    if not analysis_finished
                    else "AI suggestion is not available for this claim yet."
                ),
                "kind": "status",
            }]


def install():
    from . import mpl_notices

    if getattr(mpl_notices, "_source_fixes_installed", False):
        return

    original_search = mpl_notices.search_claim_sources
    original_serialize = mpl_notices.serialize_notice

    def search_claim_sources(notice, identifiers, issue_map=None):
        return _normalize_and_dedupe_matches(
            original_search(notice, identifiers, issue_map=issue_map)
        )

    def serialize_notice(notice, detail=False):
        data = original_serialize(notice, detail=detail)
        if detail and data.get("source_matches"):
            data["source_matches"] = _normalize_and_dedupe_matches(data["source_matches"])
        if detail:
            _apply_ai_claim_reports(data, notice)
        return data

    mpl_notices.internal_claim_number_from_source = strict_internal_claim_number_from_source
    mpl_notices.search_claim_sources = search_claim_sources
    mpl_notices.serialize_notice = serialize_notice
    mpl_notices._source_fixes_installed = True
