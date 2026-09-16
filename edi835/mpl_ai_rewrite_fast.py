"""Fast AI rewrite path for MPL recommendations.

Python remains authoritative for recommendation content. The local model is asked
once per claim to turn the approved Python recommendations into natural,
professional AI wording. It may consolidate repetitive actions for readability,
but it may not invent facts, causes, diagnoses, outcomes, or new actions.
"""

from __future__ import annotations

import json
import logging
import os

from urllib.error import HTTPError, URLError

from .mpl_ai_suggestions import (
    DEFAULT_QWEN_BASE_URL,
    DEFAULT_QWEN_MODEL,
    _clean_model_text,
    _discover_live_model,
    _qwen_headers,
    _qwen_text,
    python_suggestions_by_claim,
    python_suggestions_for_notice,
)
from .mpl_notices import local_ai_enabled

logger = logging.getLogger(__name__)


def _parse_natural_rewrite(text):
    """Accept a compact JSON response with one paragraph and one or more bullets."""
    cleaned = _clean_model_text(text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None

    if not isinstance(parsed, dict):
        return None
    paragraph = str(parsed.get("paragraph") or "").strip()
    raw_bullets = parsed.get("bullets") or []
    if not isinstance(raw_bullets, list):
        return None
    bullets = [str(value or "").strip() for value in raw_bullets if str(value or "").strip()]
    if not paragraph or not bullets:
        return None
    return {"paragraph": paragraph, "bullets": bullets}


def _rewrite_one_claim_single_call(base_url, model_id, headers, claim_number, suggestions):
    """Create natural AI wording for one claim using exactly one model request."""
    prompt = (
        "/no_think\n"
        "You are writing an operational recommendation for a healthcare EDI/claims team. "
        "The supplied recommendations were already approved by deterministic Python rules and are the only actions you may use. "
        "Rewrite them into natural, concise, professional AI wording. "
        "You may combine repetitive or closely related recommendations so the result reads like a thoughtful analyst wrote it, "
        "but do not add new actions, facts, causes, diagnoses, outcomes, assumptions, or guarantees. "
        "Keep action items as instructions using verbs such as Review, Confirm, Verify, Compare, Correct, Reprocess, or Regenerate. "
        "Do not turn an instruction into an unsupported statement of fact. "
        "Return ONLY valid JSON with exactly two keys: paragraph and bullets. "
        "paragraph should be a short professional synthesis of what should be done and why, based only on the approved recommendations. "
        "bullets should be a concise action plan, normally 3 to 8 bullets, combining overlap where useful."
    )
    payload = json.dumps(
        {
            "claim_number": str(claim_number or ""),
            "approved_recommendations": list(suggestions),
        },
        ensure_ascii=False,
    )
    max_tokens = int(os.getenv("MPL_AI_CLAIM_MAX_TOKENS", "700"))
    text = _qwen_text(base_url, model_id, headers, prompt, payload, max_tokens)
    parsed = _parse_natural_rewrite(text)
    if not parsed:
        raise ValueError("AI did not return a usable paragraph and action list.")
    return parsed


def rewrite_python_suggestions_fast(notice):
    """Rewrite Python recommendations with exactly one model call per claim."""
    groups = python_suggestions_by_claim(notice)
    flat_suggestions = python_suggestions_for_notice(notice)
    notice.ai_suggestions = flat_suggestions

    if not groups or not local_ai_enabled():
        notice.ai_response = ""
        notice.ai_response_source = ""
        notice.save(update_fields=["ai_suggestions", "ai_response", "ai_response_source", "updated_at"])
        return None

    base_url = os.getenv("MPL_AI_BASE_URL", DEFAULT_QWEN_BASE_URL).rstrip("/")
    preferred_model = os.getenv("MPL_AI_MODEL", DEFAULT_QWEN_MODEL).strip() or DEFAULT_QWEN_MODEL
    headers = _qwen_headers(base_url)
    model_id = _discover_live_model(base_url, headers, preferred_model)

    logger.info(
        "MPL AI single-call suggestion rewrite starting: notice=%s model=%s groups=%s",
        notice.pk,
        model_id,
        len(groups),
    )

    claims = []
    failures = []
    for group in groups:
        claim_number = group["claim_number"]
        suggestions = group["python_suggestions"]
        try:
            rewritten = _rewrite_one_claim_single_call(
                base_url,
                model_id,
                headers,
                claim_number,
                suggestions,
            )
            claims.append({
                "claim_number": claim_number,
                "paragraph": rewritten["paragraph"],
                "bullets": rewritten["bullets"],
            })
            logger.info(
                "MPL AI suggestion rewrite completed for claim %s in one inference call with %s action bullet(s).",
                claim_number,
                len(rewritten["bullets"]),
            )
        except (HTTPError, URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{claim_number}: {type(exc).__name__}: {exc}")
            logger.exception("MPL AI suggestion rewrite failed for %s", claim_number)

    if not claims:
        notice.ai_response = ""
        notice.ai_response_source = ""
        notice.save(update_fields=["ai_suggestions", "ai_response", "ai_response_source", "updated_at"])
        logger.error(
            "MPL AI suggestion rewrite produced no claim output for notice %s: %s",
            notice.pk,
            "; ".join(failures) or "no groups",
        )
        return None

    if failures:
        logger.warning("MPL AI suggestion rewrite completed partially: %s", "; ".join(failures))

    notice.ai_response = json.dumps({"claims": claims}, ensure_ascii=False)
    notice.ai_response_source = model_id
    notice.save(update_fields=["ai_suggestions", "ai_response", "ai_response_source", "updated_at"])
    return claims
