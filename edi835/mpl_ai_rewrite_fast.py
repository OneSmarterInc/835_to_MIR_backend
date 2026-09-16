"""Fast AI rewrite path for MPL recommendations.

Python remains authoritative for recommendation content. The local model is asked
once per claim to return one paragraph plus one bullet for each approved Python
recommendation. No per-bullet inference fallback is used.
"""

from __future__ import annotations

import json
import logging
import os

from urllib.error import HTTPError, URLError

from .mpl_ai_suggestions import (
    DEFAULT_QWEN_BASE_URL,
    DEFAULT_QWEN_MODEL,
    _discover_live_model,
    _parse_claim_rewrite,
    _qwen_headers,
    _qwen_text,
    python_suggestions_by_claim,
    python_suggestions_for_notice,
)
from .mpl_notices import local_ai_enabled

logger = logging.getLogger(__name__)


def _rewrite_one_claim_single_call(base_url, model_id, headers, claim_number, suggestions):
    """Return one paragraph and N bullets from exactly one local-model request."""
    prompt = (
        "/no_think\n"
        "You are a professional copy editor. Rewrite only the approved recommendations supplied by Python. "
        "Do not analyze the claim and do not add facts, causes, diagnoses, outcomes, or new advice. "
        "Return ONLY valid JSON with exactly two keys: paragraph and bullets. "
        "paragraph must be one concise professional paragraph covering the same recommendations. "
        "bullets must be an array with exactly one concise rewritten sentence for each input recommendation, "
        "in the same order and with the same meaning."
    )
    payload = json.dumps(
        {
            "claim_number": str(claim_number or ""),
            "approved_recommendations": list(suggestions),
            "required_bullet_count": len(suggestions),
        },
        ensure_ascii=False,
    )
    max_tokens = int(os.getenv("MPL_AI_CLAIM_MAX_TOKENS", "700"))
    text = _qwen_text(base_url, model_id, headers, prompt, payload, max_tokens)
    parsed = _parse_claim_rewrite(text, len(suggestions))
    if not parsed:
        raise ValueError("AI did not return the required paragraph and bullet count.")
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
                "MPL AI suggestion rewrite completed for claim %s in one inference call with %s bullet(s).",
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
