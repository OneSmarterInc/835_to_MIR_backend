"""Efficient AI restatement of deterministic MPL recommendations.

The deterministic Python analysis remains authoritative. The local model receives
only approved recommendation text and returns one paragraph plus one bullet per
input recommendation. Each claim uses a single inference request.
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
    _parse_claim_rewrite,
    _qwen_headers,
    python_suggestions_by_claim,
    python_suggestions_for_notice,
)
from .mpl_notices import _qwen_chat_completion, local_ai_enabled

logger = logging.getLogger(__name__)


def _rewrite_one_claim(base_url, model_id, headers, claim_number, suggestions):
    """Rewrite all approved recommendations for one claim in one model call."""
    expected_count = len(suggestions)
    if not expected_count:
        return {"paragraph": "", "bullets": []}

    system_prompt = (
        "/no_think\n"
        "You are a professional copy editor. You will receive approved recommendations "
        "created by deterministic claim rules. Do not analyze the claim and do not add, "
        "remove, merge, or invent recommendations. Preserve the meaning of every item. "
        "Return valid JSON only with exactly two keys: paragraph and bullets. "
        "paragraph must be one concise professional paragraph covering all supplied items. "
        f"bullets must be an array containing exactly {expected_count} concise sentences, "
        "in the same order as the supplied recommendations."
    )
    user_payload = json.dumps({
        "claim_number": claim_number,
        "approved_recommendations": suggestions,
        "required_bullet_count": expected_count,
    }, ensure_ascii=False)
    payload = json.dumps({
        "model": model_id,
        "temperature": 0.0,
        "max_tokens": int(os.getenv("MPL_AI_CLAIM_MAX_TOKENS", "700")),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ],
    }).encode("utf-8")

    outer = _qwen_chat_completion(base_url, payload, headers)
    raw_text = outer["choices"][0]["message"]["content"]
    rewritten = _parse_claim_rewrite(_clean_model_text(raw_text), expected_count)
    if not rewritten:
        raise ValueError(
            f"AI response did not contain one paragraph and exactly {expected_count} bullets."
        )
    return rewritten


def rewrite_python_suggestions(notice):
    """Professionally restate Python-approved suggestions using one call per claim."""
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
        "MPL AI suggestion rewrite starting: notice=%s model=%s groups=%s",
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
            rewritten = _rewrite_one_claim(
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
