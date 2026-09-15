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


def python_suggestions_for_notice(notice):
    """Collect the Python-approved recommendations already stored for a notice."""
    suggestions = []

    def add(value):
        text = _suggestion_text(value)
        if text and text not in suggestions:
            suggestions.append(text)

    links = (
        notice.notice_claims.select_related("analysis")
        .all()
        .order_by("id")
    )
    for link in links:
        analysis = getattr(link, "analysis", None)
        if not analysis:
            continue
        for item in analysis.recommended_actions or []:
            add(item)

    # Notices without a normalized 837 match still have deterministic Python
    # recommendations stored directly on the notice.
    if not suggestions:
        for item in notice.ai_suggestions or []:
            add(item)

    return suggestions[:30]


def _clean_model_text(value):
    text = str(value or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.S).strip()
    text = re.sub(r"^```(?:text|markdown)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    return text


def rewrite_python_suggestions(notice):
    """Ask Qwen only to professionally restate Python-generated suggestions.

    The model receives no email, claim, source-file, or rule-engine evidence.
    This deliberately prevents the prior large prompt from turning the model
    into a second claims-analysis engine.
    """
    suggestions = python_suggestions_for_notice(notice)
    notice.ai_suggestions = suggestions

    if not suggestions or not local_ai_enabled():
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
    prompt = (
        "/no_think\n"
        "Rewrite the supplied Python-generated suggestions professionally. "
        "Return only a numbered list. Use one concise paragraph per point. "
        "Preserve the meaning and order of every suggestion. Do not add, remove, "
        "merge, infer, or invent any fact, recommendation, diagnosis, or claim detail."
    )
    payload = json.dumps({
        "model": model_id,
        "temperature": 0.1,
        "max_tokens": int(os.getenv("MPL_AI_SUGGESTION_MAX_TOKENS", "700")),
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps({"python_suggestions": suggestions}, ensure_ascii=False)},
        ],
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if os.getenv("MPL_AI_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['MPL_AI_API_KEY']}"

    try:
        outer = _qwen_chat_completion(base_url, payload, headers)
        text = _clean_model_text(outer["choices"][0]["message"]["content"])
        if not text:
            raise ValueError("Qwen returned an empty suggestion rewrite.")
    except (HTTPError, URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("MPL Qwen suggestion rewrite failed: %s", exc)
        notice.ai_response = ""
        notice.ai_response_source = ""
        notice.save(update_fields=["ai_suggestions", "ai_response", "ai_response_source", "updated_at"])
        return None

    notice.ai_response = text
    notice.ai_response_source = model_id
    notice.save(update_fields=["ai_suggestions", "ai_response", "ai_response_source", "updated_at"])
    return text
