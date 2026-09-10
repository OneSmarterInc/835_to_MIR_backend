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

from .models import EDI837Claim, MIRClaim, MPLClaimAnalysis, MPLNotice, MPLNoticeClaim, RECONClaim


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
    identifiers = {str(value).strip() for value in (supplied or []) if str(value).strip()}
    for pattern in (
        r"(?:claim|clm|icn|internal claim|highmark claim)(?:\s+(?:number|no|#))?\s*[:#=-]?\s*([A-Z0-9][A-Z0-9_-]{4,99})",
        r"\b(CLM[A-Z0-9_-]{3,96})\b",
        r"\b(\d{17})\b",
    ):
        identifiers.update(re.findall(pattern, text or "", re.I))
    return sorted(value.upper() for value in identifiers)


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
    text = clean_email_for_analysis(body)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    target = str(claim_number or "").upper()
    positions = [index for index, line in enumerate(lines) if target and target in line.upper()]
    if not positions:
        return text[:1800]
    excerpts = []
    for position in positions[:3]:
        excerpt = "\n".join(lines[max(0, position - 8):min(len(lines), position + 5)])
        if excerpt not in excerpts:
            excerpts.append(excerpt)
    return "\n\n".join(excerpts)[:2400]


def match_claims(notice):
    identifiers = extract_claim_identifiers(
        f"{notice.subject}\n{notice.latest_message_body}",
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


def call_local_model(notice, claim, timeline, findings, actions):
    base_url = os.getenv("MPL_AI_BASE_URL", "").rstrip("/")
    if not base_url:
        return None
    model_id = os.getenv("MPL_AI_MODEL", "qwen3-0.6b-instruct-q4_k_m")
    evidence = {
        "email": {"program": notice.program, "period_start": str(notice.reporting_period_start), "period_end": str(notice.reporting_period_end), "reported_issue_context": claim_email_context(notice.latest_message_body, claim.claim_control_number)},
        "claim": {"claim_number": claim.claim_control_number, "status": "under_review"},
        "timeline": timeline, "verified_findings": findings,
        "approved_actions": [{"number": index + 1, "text": action} for index, action in enumerate(actions)],
    }
    payload = json.dumps({
        "model": model_id, "temperature": 0.0, "max_tokens": 800, "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "/no_think\nAnalyze one healthcare claim. Treat reported_issue_context as the sender's unverified report, and timeline plus verified_findings as application evidence. Explain whether the stored 837, MIR, 835, and reconciliation evidence supports the report. Suggest only review or correction steps grounded in supplied evidence and approved_actions. Return JSON only. Never invent claims, files, facts, actions, or guarantee approval. Required keys: summary, primary_issue_code, explanation, needs_response, recommended_actions, confidence, requires_human_review."},
            {"role": "user", "content": json.dumps(evidence)},
        ],
    }).encode()
    headers = {"Content-Type": "application/json"}
    if os.getenv("MPL_AI_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['MPL_AI_API_KEY']}"
    try:
        request = Request(f"{base_url}/chat/completions", data=payload, headers=headers, method="POST")
        with urlopen(request, timeout=int(os.getenv("MPL_AI_TIMEOUT_SECONDS", "120"))) as response:
            outer = json.loads(response.read().decode())
        content = outer["choices"][0]["message"]["content"]
        result = json.loads(content) if isinstance(content, str) else content
    except (HTTPError, URLError, TimeoutError, KeyError, ValueError, json.JSONDecodeError):
        return None
    allowed_codes = {item["code"] for item in findings}
    if not isinstance(result, dict) or result.get("primary_issue_code", "") not in allowed_codes | {""}:
        return None
    valid_actions = []
    for item in result.get("recommended_actions", []):
        try:
            number = int(item.get("catalogue_action_number"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= number <= len(actions):
            valid_actions.append({"catalogue_action_number": number, "explanation": str(item.get("explanation") or actions[number - 1])})
    result.update({"recommended_actions": valid_actions, "requires_human_review": True, "model_id": model_id})
    return result


def process_notice(notice_id):
    notice = MPLNotice.objects.select_related("client").get(pk=notice_id)
    notice.status, notice.processing_started_at = "PARSING_EMAIL", timezone.now()
    notice.attempt_count += 1
    notice.last_error = ""
    notice.save()
    try:
        parsed = parse_subject(notice.subject, notice.reporting_year, notice.received_at)
        notice.reporting_period_start, notice.reporting_period_end = parsed["period_start"], parsed["period_end"]
        notice.program, notice.notice_type = parsed["program"], parsed["notice_type"]
        notice.latest_message_body, notice.quoted_email_history = split_latest_message(notice.raw_email_body)
        notice.latest_message_body = clean_email_for_analysis(notice.latest_message_body)
        notice.status = "MATCHING_CLAIMS"
        notice.save()
        confirmed_links = list(notice.notice_claims.filter(confirmed_by_user=True).select_related("claim"))
        matches = [link.claim for link in confirmed_links] or match_claims(notice)
        if not matches:
            notice.status, notice.last_error = "REVIEW_REQUIRED", "None of the claim numbers in this email matched stored 837 claim data for this client."
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
            if re.search(r"\bno\s+prefix\b|\bprefix\s+(?:is\s+)?missing\b", notice.latest_message_body, re.I):
                findings.append(_finding("NO_PREFIX_NOTICE", "error", "The MPL email reports that the returned claim has no prefix.", "MPL email"))
            actions = approved_actions(findings)
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
    data = {"id": str(notice.id), "client_id": str(notice.client_id), "client_name": notice.client.name, "subject": notice.subject, "sender": notice.sender_text, "received_at": notice.received_at.isoformat() if notice.received_at else None, "period_start": str(notice.reporting_period_start) if notice.reporting_period_start else None, "period_end": str(notice.reporting_period_end) if notice.reporting_period_end else None, "program": notice.program, "notice_type": notice.notice_type, "status": notice.status, "last_error": notice.last_error, "created_at": notice.created_at.isoformat(), "source_filename": notice.source_filename, "source_file_url": f"/edi835/api/mpl-notices/{notice.id}/source-file/" if notice.source_file else None}
    if detail:
        data.update({"email_body": notice.raw_email_body, "latest_message": notice.latest_message_body, "claims": [claim_summary(link) for link in notice.notice_claims.select_related("claim", "analysis").all()]})
    return data
