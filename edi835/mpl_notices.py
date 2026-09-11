"""Evidence-first MPL notice processing for the local Qwen service."""

import json
import os
import re
from datetime import date
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.db.models import Q
from django.utils import timezone

from .models import EDI835File, EDI837Claim, MIRClaim, MPLClaimAnalysis, MPLNotice, MPLNoticeClaim, RECONClaim


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



def search_claim_sources(notice, identifiers):
    results = []
    for identifier in identifiers[:50]:
        sources = []
        claim_837 = (
            EDI837Claim.objects.filter(client=notice.client)
            .filter(
                Q(claim_control_number__iexact=identifier)
                | Q(highmark_claim_number__iexact=identifier)
                | Q(internal_claim_number__iexact=identifier)
                | Q(reference_9c__iexact=identifier)
                | Q(patient_control_number__iexact=identifier)
            )
            .select_related("edi_file")
            .order_by("-edi_file__uploaded_at", "-id")
            .first()
        )
        if claim_837:
            source = claim_837.edi_file
            sources.append({
                "type": "837",
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

        mir_claim = (
            MIRClaim.objects.filter(
                mir_file__client=notice.client,
                claim_control_number__iexact=identifier,
            )
            .select_related("mir_file")
            .order_by("-mir_file__converted_at", "-id")
            .first()
        )
        if mir_claim:
            source = mir_claim.mir_file
            sources.append({
                "type": "MIR",
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
            sources.append({
                "type": "835",
                "filename": source.original_filename,
                "status": source.status,
                "date": source.uploaded_at.isoformat() if source.uploaded_at else None,
                "details": {
                    "claims": source.claims_count,
                    "services": source.services_count,
                },
                "download_url": f"/edi835/api/mpl-files/835/{source.id}/download/",
            })

        recon_claim = (
            RECONClaim.objects.filter(client=notice.client)
            .filter(
                Q(claim_control_number__iexact=identifier)
                | Q(patient_control_number__iexact=identifier)
            )
            .select_related("recon_file")
            .order_by("-recon_file__uploaded_at", "-id")
            .first()
        )
        if recon_claim:
            source = recon_claim.recon_file
            sources.append({
                "type": "RECON",
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
        results.append({"claim_number": identifier, "sources": sources})
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
                | Q(highmark_claim_number__iexact=identifier)
                | Q(internal_claim_number__iexact=identifier)
                | Q(reference_9c__iexact=identifier)
                | Q(patient_control_number__iexact=identifier)
            )
            .select_related("edi_file")
            .order_by("-edi_file__uploaded_at", "-id")
            .first()
        )
        if claim and claim.id not in seen:
            seen.add(claim.id)
            matches.append(claim)
    return matches


def _finding(code, severity, description, evidence):
    return {"code": code, "severity": severity, "description": description, "evidence": evidence}


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
            "text": email_context,
            "issue_codes": list(issue_rules),
            "approved_issue_rules": issue_rules,
        },
        "source_timeline": timeline[-8:],
        "verified_findings": findings[:8],
        "approved_actions": [
            {"id": f"A{index + 1}", "text": action}
            for index, action in enumerate(actions[:10])
        ],
    }
    system_prompt = (
        "/no_think\n"
        "Analyze exactly one healthcare claim. The email report is an allegation; "
        "verified_findings and source_timeline are application evidence. "
        "approved_issue_rules define only the listed email codes. Unknown codes require manual review. "
        "Use only supplied facts. Never invent claim numbers, filenames, statuses, amounts, code meanings, "
        "or corrective actions. Never guarantee approval. Choose actions only by approved_actions id. "
        "If evidence is absent or conflicting, say so. Return one JSON object with exactly these keys: "
        "summary, reported_issue, verified_evidence, missing_evidence, primary_issue_code, "
        "needs_response, recommended_actions, confidence, requires_human_review. "
        "verified_evidence and missing_evidence are arrays of short strings. recommended_actions is an "
        "array of objects containing action_id and reason. confidence is a number from 0 to 1. "
        "requires_human_review must be true."
    )
    example_input = {
        "claim_number": "CLAIM_A",
        "email_report": {"text": "UE999 reported.", "issue_codes": ["UE999"], "approved_issue_rules": {}},
        "source_timeline": [],
        "verified_findings": [],
        "approved_actions": [{"id": "A1", "text": "Review the email and evidence manually."}],
    }
    example_output = {
        "summary": "The email reports an issue, but no application evidence or approved code definition was supplied.",
        "reported_issue": "UE999 was reported but is not defined by an approved rule.",
        "verified_evidence": [],
        "missing_evidence": ["Source claim evidence and an approved definition for UE999 are missing."],
        "primary_issue_code": "",
        "needs_response": True,
        "recommended_actions": [{"action_id": "A1", "reason": "The issue cannot be verified automatically."}],
        "confidence": 0.2,
        "requires_human_review": True,
    }
    payload = json.dumps({
        "model": model_id,
        "temperature": 0.0,
        "max_tokens": 450,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(example_input)},
            {"role": "assistant", "content": json.dumps(example_output)},
            {"role": "user", "content": json.dumps(evidence)},
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
    except (HTTPError, URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError):
        return None

    required_keys = {
        "summary", "reported_issue", "verified_evidence", "missing_evidence",
        "primary_issue_code", "needs_response", "recommended_actions",
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

    if not isinstance(result.get("verified_evidence"), list) or not isinstance(
        result.get("missing_evidence"), list
    ):
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

def call_unmatched_notice_model(notice, identifiers, source_matches):
    base_url = (
        os.getenv("MPL_AI_BASE_URL", "").rstrip("/")
        if local_ai_enabled()
        else ""
    )
    fallback = {
        "summary": (
            f"Extracted {len(identifiers)} claim number(s) from the email. None matched stored "
            "837 data for this client, so the reported issues cannot yet be verified against an 837 claim."
        ),
        "suggestions": [
            "Confirm that the correct client is selected.",
            "Confirm that the relevant 837 files have been uploaded and processed.",
            "Review any MIR, 835, or reconciliation source matches shown below before taking corrective action.",
        ],
        "source": "deterministic-fallback",
    }
    if not base_url:
        return fallback

    contexts = [
        claim_email_context(notice_email_body(notice), identifier)
        for identifier in identifiers[:8]
    ]
    reported_email = "\n\n".join(dict.fromkeys(item for item in contexts if item))[:3000]
    if not reported_email:
        reported_email = clean_email_for_analysis(notice_email_body(notice))[:3000]

    compact_matches = [
        {
            "claim_number": item.get("claim_number"),
            "sources": [
                {
                    "type": source.get("type"),
                    "filename": source.get("filename"),
                    "status": source.get("status"),
                    "details": source.get("details"),
                }
                for source in item.get("sources", [])[:4]
            ],
        }
        for item in source_matches[:12]
    ]
    model_id = os.getenv("MPL_AI_MODEL", "qwen3-0.6b-instruct-q8_0")
    evidence = {
        "reported_email": reported_email,
        "extracted_claim_numbers": identifiers[:50],
        "source_matches": compact_matches,
        "database_result": (
            "Claim numbers were extracted from the email, but none matched stored 837 claim data. "
            "MIR, 835, or reconciliation matches may still be listed in source_matches."
        ),
    }
    payload = json.dumps({
        "model": model_id,
        "temperature": 0.0,
        "max_tokens": 350,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "/no_think\nSummarize the sender's reported claim issues and suggest safe, concrete "
                    "investigation steps using only the supplied evidence. If extracted_claim_numbers is "
                    "non-empty, explicitly say claims were extracted; never say the email contains no claim "
                    "data. Distinguish extraction from the absence of a stored 837 match. Mention MIR, 835, "
                    "or reconciliation matches only when present in source_matches. Do not invent facts, "
                    "corrections, or guarantee approval. Return JSON only with keys summary and suggestions; "
                    "suggestions must be an array of short, actionable strings."
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

        suggestions = []
        seen = set()
        for item in result.get("suggestions", []):
            suggestion = str(item).strip()
            key = re.sub(r"\W+", " ", suggestion.lower()).strip()
            if (
                not suggestion
                or not key
                or key in seen
                or re.match(r"^no claim data (?:was|is)", suggestion, re.I)
            ):
                continue
            seen.add(key)
            suggestions.append(suggestion)
            if len(suggestions) == 8:
                break
        return {
            "summary": summary,
            "suggestions": suggestions or fallback["suggestions"],
            "source": model_id,
        }
    except (HTTPError, URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError):
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
        all_source_matches = search_claim_sources(notice, identifiers)
        notice.source_matches = [
            item for item in all_source_matches if item.get("sources")
        ]
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
            elif notice.source_matches:
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
        data["analysis"] = {"summary": analysis.summary, "primary_issue_code": analysis.primary_issue_code, "needs_response": analysis.needs_response, "confidence": float(analysis.confidence), "timeline": analysis.timeline, "findings": analysis.findings, "recommended_actions": analysis.recommended_actions, "related_files": analysis.related_files, "review_status": analysis.review_status, "model_id": analysis.model_id}
    return data


def serialize_notice(notice, detail=False):
    data = {"id": str(notice.id), "client_id": str(notice.client_id), "client_name": notice.client.name, "subject": notice.subject, "sender": notice.sender_text, "received_at": notice.received_at.isoformat() if notice.received_at else None, "period_start": str(notice.reporting_period_start) if notice.reporting_period_start else None, "period_end": str(notice.reporting_period_end) if notice.reporting_period_end else None, "program": notice.program, "notice_type": notice.notice_type, "status": notice.status, "last_error": notice.last_error, "created_at": notice.created_at.isoformat(), "source_filename": notice.source_filename, "source_file_url": f"/edi835/api/mpl-notices/{notice.id}/source-file/" if notice.source_file else None, "extracted_claim_numbers": notice.extracted_claim_numbers, "source_matches": notice.source_matches, "ai_response": notice.ai_response, "ai_response_source": notice.ai_response_source, "ai_suggestions": notice.ai_suggestions}
    if detail:
        data.update({"email_body": notice_email_body(notice), "latest_message": notice.latest_message_body, "claims": [claim_summary(link) for link in notice.notice_claims.select_related("claim", "analysis").all()]})
    return data
