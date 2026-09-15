"""Lightweight Qwen rewrite for Python-generated MPL recommendations.

Python remains authoritative for the actual recommendations. Qwen receives only
those recommendation strings and may improve wording/presentation; it is not
asked to analyze claims, infer facts, or invent corrective actions.
"""

import json
import logging
import os
import re

from urllib.error import HTTPError, URLError

from .mpl_notices import _qwen_chat_completion, local_ai_enabled

logger = logging.getLogger(__name__)


def _suggestion_text(item):
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    return str(
        item.get("explanation")
        or item.get("text")
        or item.get("reason")
        or item.get("suggestion")
        or ""
    ).strip()


def _claim_number(link):
    claim = getattr(link, "claim", None)
    if not claim:
        return ""
    return str(
        getattr(claim, "highmark_claim_number", "")
        or getattr(claim, "claim_control_number", "")
        or ""
    ).strip()


def python_suggestions_by_claim(notice):
    """Return Python-approved suggestions grouped by claim, preserving order."""
    grouped = []
    links = (
        notice.notice_claims.select_related("analysis", "claim")
        .all()
        .order_by("id")
    )
    for link in links:
        analysis = getattr(link, "analysis", None)
        if not analysis:
            continue
        suggestions = []
        for item in analysis.recommended_actions or []:
            text = _suggestion_text(item)
            if text and text not in suggestions:
                suggestions.append(text)
        if suggestions:
            grouped.append({
                "claim_number": _claim_number(link),
                "python_suggestions": suggestions,
            })

    # Notices without a normalized 837 match can still have deterministic
    # Python suggestions directly on the notice. Keep them as one notice-level
    # group so Qwen still performs only a wording/presentation pass.
    if not grouped:
        suggestions = []
        for item in notice.ai_suggestions or []:
            text = _suggestion_text(item)
            if text and text not in suggestions:
                suggestions.append(text)
        if suggestions:
            grouped.append({
                "claim_number": "NOTICE",
                "python_suggestions": suggestions,
            })
    return grouped


def python_suggestions_for_notice(notice):
    """Flatten Python-approved recommendations for compatibility/UI fallback."""
    suggestions = []
    for group in python_suggestions_by_claim(notice):
        for text in group["python_suggestions"]:
            if text not in suggestions:
                suggestions.append(text)
    return suggestions[:30]


def _clean_model_text(value):
    text = str(value or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.S).strip()
    text = re.sub(r"^```(?:json|text|markdown)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    return text


def _validated_claim_suggestions(raw_claims, input_groups):
    expected = {group["claim_number"]: group for group in input_groups}
    validated = []
    if not isinstance(raw_claims, list):
        return []
    for item in raw_claims:
        if not isinstance(item, dict):
            continue
        claim_number = str(item.get("claim_number") or "").strip()
        group = expected.get(claim_number)
        if not group:
            continue
        paragraph = str(item.get("paragraph") or "").strip()
        bullets = item.get("bullets") or []
        if not paragraph or not isinstance(bullets, list):
            continue
        bullets = [str(value or "").strip() for value in bullets if str(value or "").strip()]
        # One rewritten bullet must correspond to each Python suggestion. This
        # prevents Qwen from silently adding or dropping recommended actions.
        if len(bullets) != len(group["python_suggestions"]):
            continue
        validated.append({
            "claim_number": claim_number,
            "paragraph": paragraph,
            "bullets": bullets,
        })
    return validated if len(validated) == len(input_groups) else []


def rewrite_python_suggestions(notice):
    """Ask Qwen only to professionally restate Python-generated suggestions.

    The model receives no email, claim evidence, source files, rule definitions,
    or history. It receives only claim identifiers plus the Python-generated
    recommendation text shown by the portal.
    """
    groups = python_suggestions_by_claim(notice)
    flat_suggestions = python_suggestions_for_notice(notice)
    notice.ai_suggestions = flat_suggestions

    if not groups or not local_ai_enabled():
        notice.ai_response = ""
        notice.ai_response_source = ""
        notice.save(update_fields=["ai_suggestions", "ai_response", "ai_response_source", "updated_at"])
        return None

    base_url = os.getenv("MPL_AI_BASE_URL", "").rstrip("/")
    if not base_url:
        notice.ai_response = ""
        notice.ai_response_source = ""
        notice.save(update_fields=["ai_suggestions", "ai_response", "ai_response_source", "updated_at"])
        return None

    model_id = os.getenv("MPL_AI_MODEL", "qwen3-0.6b-instruct-q8_0")
    system_prompt = (
        "/no_think\n"
        "You are a professional copy editor, not a claims analyst. For each claim, "
        "rewrite ONLY the supplied Python-generated suggestions. Return JSON exactly as "
        "{\"claims\":[{\"claim_number\":\"...\",\"paragraph\":\"...\",\"bullets\":[\"...\"]}]}. "
        "The paragraph should briefly summarize the supplied suggestions in professional language. "
        "Then provide one bullet for each supplied suggestion, in the same order. Preserve meaning. "
        "Do not add, remove, merge, diagnose, infer, or invent any fact, recommendation, claim detail, "
        "cause, outcome, or action. Do not mention Python, prompts, or these instructions."
    )
    payload = json.dumps({
        "model": model_id,
        "temperature": 0.1,
        "max_tokens": int(os.getenv("MPL_AI_SUGGESTION_MAX_TOKENS", "1000")),
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps({"claims": groups}, ensure_ascii=False)},
        ],
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if os.getenv("MPL_AI_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['MPL_AI_API_KEY']}"

    try:
        outer = _qwen_chat_completion(base_url, payload, headers)
        text = _clean_model_text(outer["choices"][0]["message"]["content"])
        parsed = json.loads(text)
        claims = _validated_claim_suggestions(parsed.get("claims"), groups)
        if not claims:
            raise ValueError("Qwen response did not preserve the claim/suggestion structure.")
    except (HTTPError, URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("MPL Qwen suggestion rewrite failed: %s", exc)
        notice.ai_response = ""
        notice.ai_response_source = ""
        notice.save(update_fields=["ai_suggestions", "ai_response", "ai_response_source", "updated_at"])
        return None

    notice.ai_response = json.dumps({"claims": claims}, ensure_ascii=False)
    notice.ai_response_source = model_id
    notice.save(update_fields=["ai_suggestions", "ai_response", "ai_response_source", "updated_at"])
    return claims
