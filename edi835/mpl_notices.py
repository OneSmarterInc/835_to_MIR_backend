"""Evidence-first MPL notice processing for the local Qwen service."""

import json
import logging
import os
import re
from datetime import date
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.db.models import Q
from django.utils import timezone

from .models import EDI835File, EDI837Claim, MIRClaim, MPLClaimAnalysis, MPLNotice, MPLNoticeClaim, RECONClaim
from admin_panel.mir_mapper_logic.rule_registry import RULE_REGISTRY

logger = logging.getLogger(__name__)


SUBJECT_PATTERN = re.compile(
    r"^(?:(?P<prefix>fw|fwd|re)\s*:\s*)?MIR\s+Back\s+to\s+the\s+TPA\s+File\s*--\s*"
    r"(?P<sm>\d{1,2})[\/_](?P<sd>\d{1,2})\s+thru\s+(?P<em>\d{1,2})[\/_](?P<ed>\d{1,2})\s*--\s*"
    r"(?P<program>ABC(?:[_/]CPT)?)(?P<ack>\s*-?\s*Acknowledged)?\s*$", re.I,
)

CORRECTION_CATALOGUE = {
    "MIR_CLAIM_MISSING": ["Confirm the claim was included in the intended conversion batch.", "Review conversion findings, correct the approved source or mapping, regenerate the MIR, and validate it before transmission."],
    "RECON_CLAIM_MISSING": ["Confirm that reconciliation covers this claim and reporting period.", "Reprocess reconciliation after verifying the claim identifiers and source files."],
    "SERVICE_COUNT_MISMATCH": ["Compare every 837 service line with the MIR output and restore any omitted lines.", "Regenerate and validate the MIR before sending it again."],
    "CHARGE_MISMATCH": ["Compare source 837 charges with the reconciliation values.", "Correct the approved source or mapping and regenerate the affected output."],
    "SERVICE_DATE_MISMATCH": ["Verify the service-date range in the source claim and returned records.", "Correct the approved source or mapping and revalidate the output."],
    "RETURNED_REJECTED": ["Review the returned reason code and evidence with the claims/EDI team.", "Apply only an approved correction, regenerate the affected output, and monitor the next payer response."],
    "NO_PREFIX_NOTICE": ["Verify the expected client/provider prefix in configuration and the generated MIR.", "Correct the approved mapping, regenerate and validate the MIR, then transmit it again."],
    "MANUAL_REVIEW": ["Review the email and related evidence with the claims/EDI team before changing or resubmitting the claim."],
}


APPROVED_EMAIL_ISSUE_RULES = {
    "MP011": {
        "meaning": "Reported timely-filing and member-liability issue.",
        "inspect": ["service and filing dates", "current reject code", "MIR1019 member liability"],
        "actions": [
            "Verify timely-filing evidence and the applicable operational rule.",
            "Confirm member liability is zero when the approved rule requires it.",
        ],
    },
    "MP013": {
        "meaning": "Reported missing group-number issue.",
        "inspect": ["837 group fields", "MIR group fields"],
        "actions": ["Compare the group number in the submitted 837 and returned MIR records."],
    },
    "MP003": {
        "meaning": "Reported allowance and liability mismatch.",
        "inspect": ["MIR1017 allowance", "MIR1018 TPA amount", "MIR1019 member liability"],
        "actions": ["Verify that the approved MIR1018 and MIR1019 values reconcile to MIR1017."],
    },
    "RR001": {
        "meaning": "Returned claim reportedly differs from the submitted 837.",
        "inspect": ["claim charges", "service-line count", "service dates"],
        "actions": ["Compare the returned record line by line with the original submitted 837."],
    },
    "UE036": {
        "meaning": "Reported room-rate acknowledgement issue.",
        "inspect": ["claim and room type", "current reject code", "applicable allowance rule"],
        "actions": ["Verify the room-rate rule and current rejection data before applying an approved correction."],
    },
    "UE084": {
        "meaning": "Reported rejection-handling issue.",
        "inspect": ["current rejection data", "payer instruction", "member liability"],
        "actions": ["Verify the requested reject-code handling against current claim evidence and approved procedures."],
    },
    "UE011": {
        "meaning": "Claim reportedly already processed or submitted as a duplicate.",
        "inspect": ["835 payment history", "reconciliation history", "duplicate submissions"],
        "actions": ["Confirm prior processing in 835 and reconciliation history before taking further action."],
    },
    "MP001": {
        "meaning": "Reported fund or member-liability amount differs from direction.",
        "inspect": ["fund amount", "member liability", "supplemental fees", "COB calculation"],
        "actions": ["Compare fund and member-liability amounts with the approved calculation and direction."],
    },
    "MP002": {
        "meaning": "Reported fund or member-liability amount differs from direction.",
        "inspect": ["fund amount", "member liability", "supplemental fees", "COB calculation"],
        "actions": ["Compare fund and member-liability amounts with the approved calculation and direction."],
    },
    "MP014": {
        "meaning": "Reported surprise-bill claim requiring manual operations review.",
        "inspect": ["claim type", "surprise-bill indicator", "processing history"],
        "actions": ["Verify the surprise-bill classification and route to the approved operations workflow."],
    },
    "UE017": {
        "meaning": "Reported UE017 error; the email requests operations handling.",
        "inspect": ["current rejection data", "837", "MIR", "835 and reconciliation history"],
        "actions": ["Verify UE017 against current evidence before routing through the approved operations workflow."],
    },
    "UE106": {
        "meaning": "Reported UE106 error requiring operations review.",
        "inspect": ["current rejection data", "837", "MIR", "835 and reconciliation history"],
        "actions": ["Verify UE106 against current evidence before operations handling."],
    },
    "UE112": {
        "meaning": "Reported UE112 error requiring operations review.",
        "inspect": ["current rejection data", "837", "MIR", "835 and reconciliation history"],
        "actions": ["Verify UE112 against current evidence before operations handling."],
    },
    "UE115": {
        "meaning": "Reported UE115 error requiring operations review.",
        "inspect": ["current rejection data", "837", "MIR", "835 and reconciliation history"],
        "actions": ["Verify UE115 against current evidence before operations handling."],
    },
}


def reported_issue_rules(body):
    """Return only approved issue definitions explicitly present in this claim context."""
    codes = []
    for code in re.findall(r"\b(?:MP|RR|UE)\d{3}\b", body or "", re.I):
        normalized = code.upper()
        if normalized not in codes:
            codes.append(normalized)
    return {
        code: APPROVED_EMAIL_ISSUE_RULES[code]
        for code in codes
        if code in APPROVED_EMAIL_ISSUE_RULES
    }


def authoritative_rule_catalog(codes):
    """Return Checks-screen rule definitions for reported codes, when implemented."""
    wanted = {str(code).upper() for code in codes}
    return {
        rule.code: {
            "name": rule.name,
            "description": rule.description,
            "severity": rule.severity.value,
            "scope": rule.scope,
            "source": rule.source,
        }
        for rule in RULE_REGISTRY.definitions()
        if rule.code in wanted
    }


def unknown_reported_codes(body):
    """List reported MPL codes that have no approved local definition."""
    reported = {
        code.upper()
        for code in re.findall(r"\b(?:MP|RR|UE)\d{3}\b", body or "", re.I)
    }
    approved = set(APPROVED_EMAIL_ISSUE_RULES) | {
        rule.code for rule in RULE_REGISTRY.definitions()
    }
    return sorted(reported - approved)


def approved_actions_for_claim(email_context, findings):
    actions = approved_actions(findings)
    for rule in reported_issue_rules(email_context).values():
        for action in rule["actions"]:
            if action not in actions:
                actions.append(action)
    return actions


class NoticeValidationError(ValueError):
    pass


def parse_subject(subject, reporting_year=None, received_at=None):
    normalized = re.sub(r"\s+", " ", (subject or "").strip())
    match = SUBJECT_PATTERN.fullmatch(normalized)
    if not match:
        raise NoticeValidationError("Subject must match 'MIR Back to the TPA File -- M/D thru M/D -- PROGRAM'.")
    year = int(reporting_year or (received_at or timezone.now()).year)
    start = date(year, int(match["sm"]), int(match["sd"]))
    end_year = year + 1 if (int(match["em"]), int(match["ed"])) < (start.month, start.day) else year
    return {
        "period_start": start,
        "period_end": date(end_year, int(match["em"]), int(match["ed"])),
        "program": match["program"].upper().replace("/", "_"),
        "notice_type": "ACKNOWLEDGEMENT" if match["ack"] else "MIR_RESULTS",
    }



def notice_email_body(notice):
    """Return the canonical body from the Microsoft Graph-shaped message."""
    normalized = notice.normalized_email or {}
    body = normalized.get("body") or {}
    content = body.get("content") if isinstance(body, dict) else ""
    return str(content or notice.raw_email_body or "").replace("\x00", "").strip()

def split_latest_message(body):
    text = (body or "").replace("\r\n", "\n").strip()
    lower = text.lower()
    positions = [lower.find(marker) for marker in ("-----original message-----", "\nfrom:", "\nsent:")]
    positions = [position for position in positions if position > 20]
    if not positions:
        return text, ""
    split_at = min(positions)
    return text[:split_at].strip(), text[split_at:].strip()


def extract_claim_identifiers(text, supplied=None):
    """Extract claim IDs without treating issue codes or prose as claims.

    MPL emails use long numeric claim numbers. Alphanumeric identifiers remain
    supported only when supplied by a trusted caller or explicitly introduced
    by a claim label (for example, "claim CLM12345").
    """
    identifiers = []
    seen = set()

    def add(value, *, allow_alphanumeric=False):
        candidate = str(value or "").strip().upper().strip(".,;:()[]{}")
        is_numeric_claim = bool(re.fullmatch(r"\d{15,25}", candidate))
        is_labeled_claim = (
            allow_alphanumeric
            and bool(re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{4,99}", candidate))
            and any(character.isdigit() for character in candidate)
        )
        if candidate and candidate not in seen and (is_numeric_claim or is_labeled_claim):
            seen.add(candidate)
            identifiers.append(candidate)

    for value in supplied or []:
        add(value, allow_alphanumeric=True)

    body = str(text or "").replace("\u00a0", " ")
    for value in re.findall(r"(?<!\d)(\d{15,25})(?!\d)", body):
        add(value)
    for value in re.findall(
        r"\bclaim(?:\s+(?:number|id))?\s*(?:#|:|-)?\s*([A-Z0-9][A-Z0-9_-]{4,99})",
        body,
        re.I,
    ):
        add(value, allow_alphanumeric=True)
    return identifiers


def extract_claim_issue_map(text):
    """Associate recurring MPL issue sections and inline codes with claim IDs."""
    issue_map = {}
    active_codes = []
    active_category = ""
    active_description = ""
    previous_claims = []

    section_patterns = (
        ("NO_PREFIX_RETURN", r"following claims were pulled|returned with no prefix"),
        ("COB_REVIEW", r"following cob claim"),
        ("ADJUSTMENT_PENDING", r"following adjustment"),
        ("NOT_PROCESSED", r"following claim did not process"),
    )

    def add_issue(claim_number, codes, category, description):
        issue = {
            "codes": list(dict.fromkeys(code.upper() for code in codes)),
            "category": category,
            "description": re.sub(r"\s+", " ", description or "").strip()[:500],
        }
        if not issue["codes"] and not issue["category"]:
            return
        items = issue_map.setdefault(claim_number, [])
        key = (
            tuple(issue["codes"]),
            issue["category"],
            issue["description"].lower(),
        )
        existing = {
            (
                tuple(item.get("codes", [])),
                item.get("category", ""),
                item.get("description", "").lower(),
            )
            for item in items
        }
        if key not in existing:
            items.append(issue)

    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in clean_email_for_analysis(text).splitlines()
        if line.strip()
    ]
    for line in lines:
        lower = line.lower()
        claims = extract_claim_identifiers(line)
        codes = [
            code.upper()
            for code in re.findall(r"\b(?:MP|RR|UE)\d{3}\b", line, re.I)
        ]

        section_category = ""
        for category, pattern in section_patterns:
            if re.search(pattern, lower):
                section_category = category
                break

        inline_category = ""
        if "inclusively priced" in lower:
            inline_category = "INCLUSIVE_PRICING"
        elif re.search(r"\bf\s*&\s*a claim\b", lower):
            inline_category = "F_AND_A"
        elif "original claim" in lower and "recon record" in lower:
            inline_category = "ADJUSTMENT_PENDING"

        if section_category and not claims:
            active_codes = []
            active_category = section_category
            active_description = line
            previous_claims = []
            continue

        if codes and not claims:
            # Outlook sometimes wraps "-- UE036" onto the line after its claim.
            if previous_claims and line.lstrip().startswith(("-", "–", "—")):
                for claim_number in previous_claims:
                    add_issue(claim_number, codes, inline_category, line)
            active_codes = list(dict.fromkeys(codes))
            active_category = inline_category
            active_description = line
            continue

        if not claims:
            if active_codes and len(active_description) < 500:
                active_description = f"{active_description} {line}".strip()
            continue

        claim_codes = codes or active_codes
        claim_category = inline_category or active_category
        description_without_claims = line
        for claim_number in claims:
            description_without_claims = description_without_claims.replace(
                claim_number, ""
            )
        description_without_claims = description_without_claims.strip(" -–—:")
        description = description_without_claims or active_description

        for claim_number in claims:
            add_issue(
                claim_number,
                claim_codes,
                claim_category,
                description,
            )
        previous_claims = claims

    return issue_map


def clean_email_for_analysis(body):
    text = (body or "").replace("\u00a0", " ").replace("\r\n", "\n")
    footer_positions = [
        text.lower().find(marker)
        for marker in (
            "_______________________________________________ this email was sent using microsoft information rights management",
            "confidentiality notice:",
        )
    ]
    footer_positions = [position for position in footer_positions if position >= 0]
    if footer_positions:
        text = text[:min(footer_positions)]
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def claim_email_context(body, claim_number):
    raw_text = (body or "").replace("\u00a0", " ").replace("\r\n", "\n")
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    target = str(claim_number or "").upper()
    positions = [index for index, line in enumerate(lines) if target and target in line.upper()]
    if not positions:
        return clean_email_for_analysis(raw_text)[:1800]
    excerpts = []
    for position in positions[:3]:
        excerpt = "\n".join(lines[max(0, position - 8):min(len(lines), position + 5)])
        if excerpt not in excerpts:
            excerpts.append(excerpt)
    return "\n\n".join(excerpts)[:1800]



def search_claim_sources(notice, identifiers, issue_map=None):
    """Find every archived source containing each extracted claim identifier.

    Some MIR and reconciliation records retain suffixes after the base claim
    number. Prefix matching is therefore allowed only inside the current
    client, while 837 raw-claim matching covers identifiers stored in REF
    segments instead of the normalized columns.
    """
    results = []
    issue_map = issue_map or {}
    for identifier in identifiers[:50]:
        sources = []
        seen_sources = set()

        # The email identifier is the Highmark claim number. Resolve the
        # corresponding internal claim number from normalized 837 database
        # records; never copy the email identifier into the internal column.
        internal_number_rows = list(
            EDI837Claim.objects.filter(
                client=notice.client,
                highmark_claim_number__iexact=identifier,
            )
            .exclude(internal_claim_number="")
            .values_list("internal_claim_number", flat=True)
            .distinct()[:10]
        )
        if not internal_number_rows:
            internal_number_rows = list(
                EDI837Claim.objects.filter(client=notice.client)
                .filter(
                    Q(claim_control_number__iexact=identifier)
                    | Q(reference_9c__iexact=identifier)
                    | Q(patient_control_number__iexact=identifier)
                    | Q(raw_claim__contains=identifier)
                )
                .exclude(internal_claim_number="")
                .values_list("internal_claim_number", flat=True)
                .distinct()[:10]
            )
        authoritative_internal_number = ", ".join(
            dict.fromkeys(str(value).strip() for value in internal_number_rows if value)
        )

        def append_source(source_type, source_id, payload):
            key = (source_type, str(source_id))
            if key not in seen_sources:
                seen_sources.add(key)
                sources.append(payload)

        claims_837 = (
            EDI837Claim.objects.filter(client=notice.client)
            .filter(
                Q(claim_control_number__iexact=identifier)
                | Q(claim_control_number__istartswith=identifier)
                | Q(highmark_claim_number__iexact=identifier)
                | Q(highmark_claim_number__istartswith=identifier)
                | Q(internal_claim_number__iexact=identifier)
                | Q(internal_claim_number__istartswith=identifier)
                | Q(reference_9c__iexact=identifier)
                | Q(reference_9c__istartswith=identifier)
                | Q(patient_control_number__iexact=identifier)
                | Q(patient_control_number__istartswith=identifier)
                | Q(raw_claim__contains=identifier)
            )
            .select_related("edi_file")
            .order_by("-edi_file__uploaded_at", "-id")[:3]
        )
        for claim_837 in claims_837:
            source = claim_837.edi_file
            append_source("837", source.id, {
                "type": "837",
                "internal_claim_number": claim_837.internal_claim_number or "",
                "filename": source.original_filename,
                "status": source.status,
                "date": source.uploaded_at.isoformat() if source.uploaded_at else None,
                "details": {
                    "service_dates": f"{claim_837.service_from_date or '—'} – {claim_837.service_to_date or '—'}",
                    "services": claim_837.service_count,
                    "total_charge": str(claim_837.total_charge_amount),
                },
                "download_url": f"/edi835/api/mpl-files/837/{source.id}/download/",
            })

        mir_claims = (
            MIRClaim.objects.filter(mir_file__client=notice.client)
            .filter(
                Q(claim_control_number__iexact=identifier)
                | Q(claim_control_number__istartswith=identifier)
                | Q(header_raw__contains=identifier)
            )
            .select_related("mir_file")
            .order_by("-mir_file__converted_at", "-id")[:3]
        )
        for mir_claim in mir_claims:
            source = mir_claim.mir_file
            append_source("MIR", source.id, {
                "type": "MIR",
                "internal_claim_number": authoritative_internal_number,
                "filename": source.mir_filename,
                "status": mir_claim.claim_status or source.status,
                "date": source.converted_at.isoformat() if source.converted_at else None,
                "details": {
                    "reason": mir_claim.primary_reason or "—",
                    "services": mir_claim.service_count,
                },
                "download_url": f"/edi835/api/mpl-files/mir/{source.id}/download/",
            })

        files_835 = (
            EDI835File.objects.filter(
                client=notice.client,
                input_file_content__contains=identifier,
            )
            .order_by("-uploaded_at")[:3]
        )
        for source in files_835:
            matched_clp_numbers = []
            for segment in re.split(r"[~\r\n]+", source.input_file_content or ""):
                fields = segment.strip().split("*")
                if fields and fields[0].upper() == "CLP" and identifier in segment:
                    # CLP01 is the submitter/patient control number for the
                    # claim represented by this 835 payment segment.
                    candidate = fields[1].strip() if len(fields) > 1 else ""
                    if candidate and candidate not in matched_clp_numbers:
                        matched_clp_numbers.append(candidate)
            append_source("835", source.id, {
                "type": "835",
                "internal_claim_number": authoritative_internal_number,
                "filename": source.original_filename,
                "status": source.status,
                "date": source.uploaded_at.isoformat() if source.uploaded_at else None,
                "details": {
                    "claims": source.claims_count,
                    "services": source.services_count,
                },
                "download_url": f"/edi835/api/mpl-files/835/{source.id}/download/",
            })

        recon_claims = (
            RECONClaim.objects.filter(client=notice.client)
            .filter(
                Q(claim_control_number__iexact=identifier)
                | Q(claim_control_number__istartswith=identifier)
                | Q(patient_control_number__iexact=identifier)
                | Q(patient_control_number__istartswith=identifier)
                | Q(raw_record__contains=identifier)
            )
            .select_related("recon_file")
            .order_by("-recon_file__uploaded_at", "-id")[:3]
        )
        for recon_claim in recon_claims:
            source = recon_claim.recon_file
            append_source("RECON", source.id, {
                "type": "RECON",
                "internal_claim_number": authoritative_internal_number,
                "filename": source.original_filename,
                "status": recon_claim.claim_status or source.status,
                "date": source.uploaded_at.isoformat() if source.uploaded_at else None,
                "details": {
                    "service_dates": f"{recon_claim.service_from_date or '—'} – {recon_claim.service_to_date or '—'}",
                    "services": recon_claim.service_count,
                    "charge": str(recon_claim.charge_amount),
                    "paid": str(recon_claim.paid_amount),
                    "patient_responsibility": str(recon_claim.patient_responsibility),
                },
                "download_url": f"/edi835/api/mpl-files/recon/{source.id}/download/",
            })

        sources.sort(key=lambda item: (
            {"837": 0, "MIR": 1, "835": 2, "RECON": 3}.get(item["type"], 9),
            item["filename"],
        ))
        results.append({
            "claim_number": identifier,
            "reported_issues": issue_map.get(identifier, []),
            "sources": sources,
        })
    return results


def match_claims(notice):
    identifiers = extract_claim_identifiers(
        f"{notice.subject}\n{notice_email_body(notice)}",
        notice.requested_claim_numbers,
    )
    matches = []
    seen = set()
    for identifier in identifiers[:50]:
        claim = (
            EDI837Claim.objects.filter(client=notice.client)
            .filter(
                Q(claim_control_number__iexact=identifier)
                | Q(claim_control_number__istartswith=identifier)
                | Q(highmark_claim_number__iexact=identifier)
                | Q(highmark_claim_number__istartswith=identifier)
                | Q(internal_claim_number__iexact=identifier)
                | Q(internal_claim_number__istartswith=identifier)
                | Q(reference_9c__iexact=identifier)
                | Q(reference_9c__istartswith=identifier)
                | Q(patient_control_number__iexact=identifier)
                | Q(patient_control_number__istartswith=identifier)
                | Q(raw_claim__contains=identifier)
            )
            .select_related("edi_file")
            .order_by("-edi_file__uploaded_at", "-id")
            .first()
        )
        if claim and claim.id not in seen:
            seen.add(claim.id)
            matches.append(claim)
    return matches


def _finding(code, severity, description, evidence, details=None):
    finding = {"code": code, "severity": severity, "description": description, "evidence": evidence}
    if details:
        finding["details"] = details
    return finding


def conversion_findings_for_claim(source_835, identifiers):
    """Select stored Checks-screen/MIR-gate findings for one claim."""
    wanted = {str(value or "").strip().upper() for value in identifiers if value}
    selected = []
    for raw in source_835.conversion_findings or []:
        if not isinstance(raw, dict):
            continue
        candidates = {
            str(raw.get(key) or "").strip().upper()
            for key in ("claim_number", "claim_control_number", "icn")
        }
        candidates.discard("")
        if wanted and not any(
            candidate in wanted
            or candidate[:17] in wanted
            or any(value[:17] == candidate[:17] for value in wanted)
            for candidate in candidates
        ):
            continue
        code = str(raw.get("rule_code") or raw.get("code") or "MIR_CHECK").strip().upper()
        description = str(raw.get("reason") or raw.get("description") or "Stored MIR conversion finding.").strip()
        selected.append(_finding(
            code,
            str(raw.get("severity") or "warning").lower(),
            description,
            f"{source_835.original_filename} · Checks/MIR rule engine",
            raw.get("evidence") or raw.get("provenance"),
        ))
    return selected


def _file_item(kind, file_id, filename, event_at, status, download_url):
    return {"type": kind, "id": str(file_id), "filename": filename, "date": event_at.isoformat() if event_at else None, "status": status, "download_url": download_url}


def collect_evidence(claim):
    timeline, files, findings = [], [], []
    source = claim.edi_file
    source_at = source.processed_at or source.uploaded_at
    timeline.append({"event": "837 processed", "date": source_at.isoformat(), "status": source.status, "file": source.original_filename})
    files.append(_file_item("837", source.id, source.original_filename, source.uploaded_at, source.status, f"/edi835/api/mpl-files/837/{source.id}/download/"))
    match_values = [value for value in (claim.claim_control_number, claim.internal_claim_number, claim.highmark_claim_number, claim.reference_9c) if value]
    mir_q, recon_q = Q(), Q()
    for value in match_values:
        mir_q |= Q(claim_control_number__iexact=value)
        recon_q |= Q(claim_control_number__iexact=value)
    mir_claims = list(MIRClaim.objects.filter(mir_file__client=claim.client).filter(mir_q).select_related("mir_file", "mir_file__source_835").prefetch_related("service_lines")[:20]) if match_values else []
    recon_claims = list(RECONClaim.objects.filter(client=claim.client).filter(recon_q).select_related("recon_file")[:20]) if match_values else []
    if not mir_claims:
        findings.append(_finding("MIR_CLAIM_MISSING", "error", "No matching MIR claim was found for this 837 claim.", "837/MIR comparison"))
    for mir in mir_claims:
        mf, source_835 = mir.mir_file, mir.mir_file.source_835
        timeline.extend([
            {"event": "MIR generated", "date": mf.converted_at.isoformat(), "status": mf.status, "file": mf.mir_filename},
            {"event": "835 received", "date": source_835.uploaded_at.isoformat(), "status": source_835.status, "file": source_835.original_filename},
        ])
        files.extend([
            _file_item("MIR", mf.id, mf.mir_filename, mf.converted_at, mf.status, f"/edi835/api/mpl-files/mir/{mf.id}/download/"),
            _file_item("835", source_835.id, source_835.original_filename, source_835.uploaded_at, source_835.status, f"/edi835/api/mpl-files/835/{source_835.id}/download/"),
        ])
        findings.extend(conversion_findings_for_claim(source_835, match_values))
        if mir.service_count != claim.service_count:
            findings.append(_finding("SERVICE_COUNT_MISMATCH", "error", f"837 has {claim.service_count} service lines while MIR has {mir.service_count}.", mf.mir_filename))
        mir_dates = {line.service_date for line in mir.service_lines.all() if line.service_date}
        claim_dates = {line.service_from_date for line in claim.service_lines.all() if line.service_from_date}
        if mir_dates and claim_dates and not (mir_dates & claim_dates):
            findings.append(_finding("SERVICE_DATE_MISMATCH", "error", "The 837 and MIR service dates do not overlap.", mf.mir_filename))
        if (mir.claim_status or "").upper() in {"R", "REJECTED", "DENIED"}:
            findings.append(_finding("RETURNED_REJECTED", "error", f"The MIR status is {mir.claim_status}; reason {mir.primary_reason or 'not supplied'}.", mf.mir_filename))
    if not recon_claims:
        findings.append(_finding("RECON_CLAIM_MISSING", "warning", "No matching reconciliation claim was found.", "reconciliation"))
    for recon in recon_claims:
        rf = recon.recon_file
        timeline.append({"event": "Reconciliation processed", "date": (rf.processed_at or rf.uploaded_at).isoformat(), "status": recon.claim_status or rf.status, "file": rf.original_filename})
        files.append(_file_item("RECON", rf.id, rf.original_filename, rf.uploaded_at, rf.status, f"/edi835/api/mpl-files/recon/{rf.id}/download/"))
        if Decimal(recon.charge_amount) != Decimal(claim.total_charge_amount):
            findings.append(_finding("CHARGE_MISMATCH", "error", f"837 charge is {claim.total_charge_amount} while reconciliation charge is {recon.charge_amount}.", rf.original_filename))
    timeline.sort(key=lambda item: item.get("date") or "")
    files = list({(item["type"], item["id"]): item for item in files}.values())
    findings = list({(item["code"], item["evidence"]): item for item in findings}.values())
    return timeline, files, findings


def approved_actions(findings):
    actions = []
    for finding in findings:
        for action in CORRECTION_CATALOGUE.get(finding["code"], CORRECTION_CATALOGUE["MANUAL_REVIEW"]):
            if action not in actions:
                actions.append(action)
    return actions or CORRECTION_CATALOGUE["MANUAL_REVIEW"]


def fallback_summary(claim, findings):
    if not findings:
        return f"Claim {claim.claim_control_number} was found. No configured rule identified a discrepancy; claims-team review is still required."
    return f"Claim {claim.claim_control_number} has {len(findings)} verified finding(s): " + "; ".join(item["description"] for item in findings[:3])



def parse_model_json(content):
    if isinstance(content, dict):
        return content
    text = str(content or "").strip()
    fenced = re.fullmatch(r"\x60\x60\x60(?:json)?\s*(.*?)\s*\x60\x60\x60", text, re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        result = json.loads(text[start:end + 1])
    if not isinstance(result, dict):
        raise ValueError("Model response must be a JSON object.")
    return result


def local_ai_enabled():
    """AI is opt-in so CPU inference cannot slow the production web server."""
    return os.getenv("MPL_AI_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def call_local_model(notice, claim, timeline, findings, actions):
    if not local_ai_enabled():
        return None
    base_url = os.getenv("MPL_AI_BASE_URL", "").rstrip("/")
    if not base_url:
        return None

    model_id = os.getenv("MPL_AI_MODEL", "qwen3-0.6b-instruct-q8_0")
    email_context = claim_email_context(
        notice_email_body(notice),
        claim.claim_control_number,
    )
    issue_rules = reported_issue_rules(email_context)
    evidence = {
        "claim_number": claim.claim_control_number,
        "email_report": {
            "text": re.sub(r"\\s+", " ", email_context).strip()[:700],
            "issue_codes": list(issue_rules),
            "approved_issue_rules": issue_rules,
            "authoritative_check_rules": authoritative_rule_catalog(issue_rules),
            "unknown_codes": unknown_reported_codes(email_context),
        },
        "source_timeline": timeline[-4:],
        "verified_findings": findings[:5],
        "approved_actions": [
            {"id": f"A{index + 1}", "text": action}
            for index, action in enumerate(actions[:6])
        ],
    }
    system_prompt = (
        "/no_think\nAnalyze one healthcare claim from supplied evidence. Priority: verified findings, "
        "authoritative check rules, approved email rules, then email wording. Email statements are unverified. "
        "Never invent facts, code meanings, or actions; disclose unknown codes, unclear items, conflicts, and missing evidence. "
        "Use approved action IDs only. Never guarantee approval. Return JSON keys: summary, reported_issue, "
        "verified_evidence, missing_evidence, unknown_codes, unclear_items, primary_issue_code, needs_response, "
        "recommended_actions, confidence, requires_human_review. Arrays must be arrays; recommended_actions items use "
        "action_id and reason; confidence is 0..1; requires_human_review is true."
    )
    payload = json.dumps({
        "model": model_id,
        "temperature": 0.0,
        "max_tokens": 280,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(evidence, separators=(",", ":"))},
        ],
    }).encode()
    headers = {"Content-Type": "application/json"}
    if os.getenv("MPL_AI_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['MPL_AI_API_KEY']}"

    try:
        request = Request(
            f"{base_url}/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )
        with urlopen(
            request,
            timeout=int(os.getenv("MPL_AI_TIMEOUT_SECONDS", "120")),
        ) as response:
            outer = json.loads(response.read().decode())
        result = parse_model_json(outer["choices"][0]["message"]["content"])
    except (HTTPError, URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("MPL claim AI request failed; using deterministic analysis: %s", exc)
        return None

    required_keys = {
        "summary", "reported_issue", "verified_evidence", "missing_evidence",
        "unknown_codes", "unclear_items", "primary_issue_code", "needs_response", "recommended_actions",
        "confidence", "requires_human_review",
    }
    if not required_keys.issubset(result):
        return None

    allowed_issue_codes = (
        {item["code"] for item in findings}
        | set(issue_rules)
    )
    if result.get("primary_issue_code", "") not in allowed_issue_codes | {""}:
        return None

    invented_claims = set(extract_claim_identifiers(json.dumps(result))) - {
        str(claim.claim_control_number).upper()
    }
    if invented_claims:
        return None

    if any(not isinstance(result.get(key), list) for key in ("verified_evidence", "missing_evidence", "unknown_codes", "unclear_items")):
        return None

    action_lookup = {
        f"A{index + 1}": action
        for index, action in enumerate(actions[:10])
    }
    valid_actions = []
    seen_action_ids = set()
    for item in result.get("recommended_actions", []):
        if not isinstance(item, dict):
            continue
        action_id = str(item.get("action_id") or "").upper()
        if action_id not in action_lookup or action_id in seen_action_ids:
            continue
        seen_action_ids.add(action_id)
        valid_actions.append({
            "action_id": action_id,
            "explanation": action_lookup[action_id],
        })

    try:
        confidence = min(max(float(result.get("confidence", 0)), 0), 1)
    except (TypeError, ValueError):
        return None

    result.update({
        "recommended_actions": valid_actions,
        "confidence": confidence,
        "requires_human_review": True,
        "model_id": model_id,
    })
    return result

UNMATCHED_CATEGORY_ACTIONS = {
    "NO_PREFIX_RETURN": "Verify the expected prefix in configuration and the generated MIR, then regenerate only after the mapping is approved.",
    "COB_REVIEW": "Compare COB amounts and responsibility fields across the 837, 835, MIR, and reconciliation record before correction.",
    "ADJUSTMENT_PENDING": "Confirm that the original claim processed and the reconciliation record finalized before closing or resubmitting the adjustment.",
    "NOT_PROCESSED": "Use the matched 835 and reconciliation status to determine why the claim did not process, then route it through the approved operations workflow.",
    "INCLUSIVE_PRICING": "Verify the applicable inclusive-pricing rule, procedure data, and allowance before making a claim change.",
    "F_AND_A": "Verify the F&A classification and processing instruction against the archived claim records before operations handling.",
}


def unmatched_notice_actions(source_matches):
    """Build valid, issue-specific actions without accepting free-form model advice."""
    actions = []
    unknown = set()
    source_types = set()

    def add(action):
        if action and action not in actions:
            actions.append(action)

    for match in source_matches:
        for source in match.get("sources", []):
            source_types.add(str(source.get("type") or "").upper())
        for issue in match.get("reported_issues", []):
            for code in issue.get("codes", []):
                normalized = str(code).upper()
                rule = APPROVED_EMAIL_ISSUE_RULES.get(normalized)
                if rule:
                    for action in rule.get("actions", []):
                        add(action)
                else:
                    unknown.add(normalized)
            add(UNMATCHED_CATEGORY_ACTIONS.get(issue.get("category")))

    if "MIR" in source_types:
        add("Compare the matched MIR claim status, reason, service count, and mapped values with the email allegation.")
    if "835" in source_types:
        add("Inspect the matched 835 claim status, adjustment reasons, payment amounts, and service lines before changing the claim.")
    if "RECON" in source_types:
        add("Confirm the matched reconciliation status, charge, paid amount, and patient responsibility before resubmission.")
    for code in sorted(unknown):
        add(f"Obtain the approved code-dictionary definition for {code}; do not infer its meaning from the email alone.")

    if not actions:
        add("Locate the corresponding 837 or obtain the approved source claim record before deciding on a correction.")
    return actions[:10]


def call_unmatched_notice_model(notice, identifiers, source_matches):
    base_url = (
        os.getenv("MPL_AI_BASE_URL", "").rstrip("/")
        if local_ai_enabled()
        else ""
    )
    approved_suggestions = unmatched_notice_actions(source_matches)
    fallback = {
        "summary": (
            f"Extracted {len(identifiers)} claim number(s) from the email. None matched stored "
            "837 data for this client, so the reported issues cannot yet be verified against an 837 claim."
        ),
        "suggestions": approved_suggestions,
        "source": "deterministic-fallback",
    }
    if not base_url:
        return fallback

    contexts = [
        claim_email_context(notice_email_body(notice), identifier)
        for identifier in identifiers[:4]
    ]
    reported_email = "\n\n".join(dict.fromkeys(item for item in contexts if item))[:900]
    if not reported_email:
        reported_email = clean_email_for_analysis(notice_email_body(notice))[:900]

    compact_matches = [
        {
            "claim_number": item.get("claim_number"),
            "reported_issues": [
                {
                    "codes": issue.get("codes", []),
                    "category": issue.get("category", ""),
                    "description": str(issue.get("description") or "")[:180],
                }
                for issue in item.get("reported_issues", [])[:2]
            ],
            "sources": [
                {
                    "type": source.get("type"),
                    "filename": source.get("filename"),
                    "status": source.get("status"),
                    "details": source.get("details"),
                }
                for source in item.get("sources", [])[:2]
            ],
        }
        for item in source_matches[:8]
    ]
    model_id = os.getenv("MPL_AI_MODEL", "qwen3-0.6b-instruct-q8_0")
    evidence = {
        "reported_email": reported_email,
        "extracted_claim_numbers": identifiers[:12],
        "source_matches": compact_matches,
        "unknown_codes": unknown_reported_codes(reported_email),
        "approved_issue_rules": reported_issue_rules(reported_email),
        "authoritative_check_rules": authoritative_rule_catalog(reported_issue_rules(reported_email)),
        "database_result": (
            "Claim numbers were extracted from the email, but none matched stored 837 claim data. "
            "MIR, 835, or reconciliation matches may still be listed in source_matches."
        ),
    }
    payload = json.dumps({
        "model": model_id,
        "temperature": 0.0,
        "max_tokens": 220,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "/no_think\nUse only supplied evidence. Claims were extracted but did not match stored 837 data. "
                    "Summarize reported issues, MIR/835/reconciliation matches, unknown codes, unclear statements, "
                    "and missing evidence. Check rules outrank email wording. Do not invent facts or promise approval. "
                    "Return JSON only with summary:string and suggestions:string[]. Suggestions are ignored; corrective actions are supplied by the application."
                ),
            },
            {"role": "user", "content": json.dumps(evidence)},
        ],
    }).encode()
    headers = {"Content-Type": "application/json"}
    if os.getenv("MPL_AI_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['MPL_AI_API_KEY']}"
    try:
        request = Request(f"{base_url}/chat/completions", data=payload, headers=headers, method="POST")
        with urlopen(
            request,
            timeout=int(os.getenv("MPL_AI_TIMEOUT_SECONDS", "120")),
        ) as response:
            outer = json.loads(response.read().decode())
        result = parse_model_json(outer["choices"][0]["message"]["content"])
        summary = str(result.get("summary") or "").strip()
        if not summary or (
            identifiers
            and re.search(r"\b(?:email contains|contains) no claim data\b", summary, re.I)
        ):
            return fallback

        return {
            "summary": summary,
            "suggestions": approved_suggestions,
            "source": model_id,
        }
    except (HTTPError, URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("MPL unmatched AI request failed; using deterministic fallback: %s", exc)
        return fallback


def process_notice(notice_id):
    notice = MPLNotice.objects.select_related("client").get(pk=notice_id)
    notice.status, notice.processing_started_at = "PARSING_EMAIL", timezone.now()
    notice.attempt_count += 1
    notice.last_error = ""
    notice.ai_response = ""
    notice.ai_response_source = ""
    notice.ai_suggestions = []
    notice.save()
    try:
        parsed = parse_subject(notice.subject, notice.reporting_year, notice.received_at)
        notice.reporting_period_start, notice.reporting_period_end = parsed["period_start"], parsed["period_end"]
        notice.program, notice.notice_type = parsed["program"], parsed["notice_type"]
        notice.latest_message_body, notice.quoted_email_history = split_latest_message(notice_email_body(notice))
        notice.latest_message_body = clean_email_for_analysis(notice.latest_message_body)
        identifiers = extract_claim_identifiers(
            f"{notice.subject}\n{notice_email_body(notice)}",
            notice.requested_claim_numbers,
        )
        issue_map = extract_claim_issue_map(notice_email_body(notice))
        all_source_matches = search_claim_sources(
            notice,
            identifiers,
            issue_map=issue_map,
        )
        # Retain every extracted claim so reported issues remain visible even
        # when no archived source matches that claim.
        notice.source_matches = all_source_matches
        # Keep every valid claim extracted from the email visible. Source
        # matching is separate: an absent database match must not erase a
        # legitimate claim number reported by the sender.
        notice.extracted_claim_numbers = identifiers
        notice.status = "MATCHING_CLAIMS"
        notice.save()
        confirmed_links = list(notice.notice_claims.filter(confirmed_by_user=True).select_related("claim"))
        matches = [link.claim for link in confirmed_links] or match_claims(notice)
        if not matches:
            unmatched_ai = call_unmatched_notice_model(notice, notice.extracted_claim_numbers, notice.source_matches)
            notice.ai_response = unmatched_ai["summary"]
            notice.ai_response_source = unmatched_ai["source"]
            notice.ai_suggestions = unmatched_ai["suggestions"]
            notice.status = "REVIEW_REQUIRED"
            if not identifiers:
                notice.last_error = "No valid claim numbers were found in the email."
            elif any(item.get("sources") for item in notice.source_matches):
                notice.last_error = (
                    "No extracted claim number matched stored 837 data for this client; "
                    "matches from other claim sources are shown below."
                )
            else:
                notice.last_error = (
                    "None of the extracted claim numbers matched stored 837, MIR, 835, "
                    "or reconciliation data for this client."
                )
            notice.processing_completed_at = timezone.now()
            notice.save()
            return notice
        if confirmed_links:
            links = confirmed_links
        else:
            notice.notice_claims.all().delete()
            links = [
                MPLNoticeClaim.objects.create(
                    notice=notice,
                    claim=claim,
                    matching_method="exact_identifier",
                    matching_confidence=Decimal("1.0"),
                    confirmed_by_user=True,
                )
                for claim in matches
            ]
        notice.status = "COLLECTING_EVIDENCE"
        notice.save()
        for link in links:
            timeline, files, findings = collect_evidence(link.claim)
            email_context = claim_email_context(notice_email_body(notice), link.claim.claim_control_number)
            claim_identifiers = {
                str(value).upper()
                for value in (
                    link.claim.claim_control_number,
                    link.claim.highmark_claim_number,
                    link.claim.internal_claim_number,
                    link.claim.reference_9c,
                    link.claim.patient_control_number,
                )
                if value
            }
            reported_issues = [
                issue
                for identifier in claim_identifiers
                for issue in issue_map.get(identifier, [])
            ]
            for unknown_code in unknown_reported_codes(email_context):
                findings.append(_finding(
                    "UNKNOWN_REPORTED_CODE",
                    "warning",
                    f"The email reports {unknown_code}, but no approved definition exists in the code dictionary.",
                    "MPL email; definition unavailable",
                    {"reported_code": unknown_code},
                ))
            for issue in reported_issues:
                label = ", ".join(issue.get("codes", [])) or issue.get("category", "REPORTED_ISSUE")
                findings.append(_finding(
                    f"REPORTED_{label.replace(', ', '_')}",
                    "warning",
                    issue.get("description") or f"The MPL email reports {label}.",
                    "MPL email (reported; verify against application evidence)",
                ))
            if re.search(r"\bno\s+prefix\b|\bprefix\s+(?:is\s+)?missing\b", email_context, re.I):
                findings.append(_finding("NO_PREFIX_NOTICE", "error", "The MPL email reports that the returned claim has no prefix.", "MPL email"))
            actions = approved_actions_for_claim(email_context, findings)
            notice.status = "ANALYZING"
            notice.save()
            ai = call_local_model(notice, link.claim, timeline, findings, actions)
            confidence = Decimal(str(min(max(float((ai or {}).get("confidence", 0.70 if findings else 0.40)), 0), 1)))
            MPLClaimAnalysis.objects.update_or_create(notice_claim=link, defaults={
                "model_id": (ai or {}).get("model_id", "deterministic-fallback"), "timeline": timeline,
                "findings": findings, "recommended_actions": (ai or {}).get("recommended_actions") or actions, "related_files": files,
                "summary": (ai or {}).get("summary") or fallback_summary(link.claim, findings),
                "primary_issue_code": (ai or {}).get("primary_issue_code") or (findings[0]["code"] if findings else ""),
                "needs_response": bool((ai or {}).get("needs_response", notice.notice_type != "ACKNOWLEDGEMENT")),
                "confidence": confidence, "raw_model_output": ai or {},
            })
        notice.status, notice.processing_completed_at = "COMPLETED", timezone.now()
        notice.save()
    except Exception as exc:
        notice.status, notice.last_error, notice.processing_completed_at = "FAILED", str(exc)[:1000], timezone.now()
        notice.save()
    return notice


def claim_summary(link):
    claim, analysis = link.claim, getattr(link, "analysis", None)
    data = {
        "link_id": link.id, "claim_id": claim.id, "claim_number": claim.claim_control_number,
        "internal_claim_number": claim.internal_claim_number, "highmark_claim_number": claim.highmark_claim_number,
        "member_id": ("*" * max(len(claim.member_id) - 4, 0) + claim.member_id[-4:]) if claim.member_id else "",
        "service_from_date": claim.service_from_date, "service_to_date": claim.service_to_date,
        "total_charge": str(claim.total_charge_amount), "matching_method": link.matching_method,
        "matching_confidence": float(link.matching_confidence), "confirmed": link.confirmed_by_user, "analysis": None,
    }
    if analysis:
        data["analysis"] = {"summary": analysis.summary, "primary_issue_code": analysis.primary_issue_code, "needs_response": analysis.needs_response, "confidence": float(analysis.confidence), "timeline": analysis.timeline, "findings": analysis.findings, "recommended_actions": analysis.recommended_actions, "related_files": analysis.related_files, "review_status": analysis.review_status, "model_id": analysis.model_id, "unknown_codes": (analysis.raw_model_output or {}).get("unknown_codes", []), "unclear_items": (analysis.raw_model_output or {}).get("unclear_items", []), "missing_evidence": (analysis.raw_model_output or {}).get("missing_evidence", [])}
    return data


def serialize_notice(notice, detail=False):
    data = {"id": str(notice.id), "client_id": str(notice.client_id), "client_name": notice.client.name, "subject": notice.subject, "sender": notice.sender_text, "received_at": notice.received_at.isoformat() if notice.received_at else None, "period_start": str(notice.reporting_period_start) if notice.reporting_period_start else None, "period_end": str(notice.reporting_period_end) if notice.reporting_period_end else None, "program": notice.program, "notice_type": notice.notice_type, "status": notice.status, "last_error": notice.last_error, "created_at": notice.created_at.isoformat(), "source_filename": notice.source_filename, "source_file_url": f"/edi835/api/mpl-notices/{notice.id}/source-file/" if notice.source_file else None, "extracted_claim_numbers": notice.extracted_claim_numbers, "source_matches": notice.source_matches, "ai_response": notice.ai_response, "ai_response_source": notice.ai_response_source, "ai_suggestions": notice.ai_suggestions}
    if detail:
        data.update({"email_body": notice_email_body(notice), "latest_message": notice.latest_message_body, "claims": [claim_summary(link) for link in notice.notice_claims.select_related("claim", "analysis").all()]})
    return data
