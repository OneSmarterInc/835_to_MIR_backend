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
from urllib.request import Request, urlopen

from .mpl_notices import _qwen_chat_completion, local_ai_enabled

logger = logging.getLogger(__name__)

DEFAULT_QWEN_BASE_URL = "http://127.0.0.1:8080/v1"
DEFAULT_QWEN_MODEL = "qwen3-0.6b-instruct-q8_0"


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


def _available_model_ids(payload):
    """Read both OpenAI-style and llama.cpp-style model-list responses."""
    rows = []
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            rows.extend(payload["data"])
        if isinstance(payload.get("models"), list):
            rows.extend(payload["models"])
    ids = []
    for row in rows:
        if isinstance(row, str):
            candidate = row.strip()
            if candidate and candidate not in ids:
                ids.append(candidate)
            continue
        if not isinstance(row, dict):
            continue
        for key in ("id", "model", "name"):
            candidate = str(row.get(key) or "").strip()
            if candidate and candidate not in ids:
                ids.append(candidate)
                break
    return ids


def _discover_live_model(base_url, headers, preferred):
    """Use the model actually exposed by the local Qwen server.

    Production has changed quantization aliases over time. A stale env alias must
    not make the MPL AI step fail when the local server is healthy.
    """
    request_headers = {
        key: value for key, value in headers.items()
        if key.lower() != "content-type"
    }
    request = Request(
        f"{base_url}/models",
        headers=request_headers,
        method="GET",
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Could not discover live Qwen model; using configured alias %s: %s", preferred, exc)
        return preferred

    available = _available_model_ids(payload)
    if not available:
        logger.warning("Qwen /models returned no usable aliases; using configured alias %s", preferred)
        return preferred
    if preferred in available:
        return preferred

    selected = available[0]
    logger.warning(
        "Configured Qwen model %s is not live; using server model %s instead.",
        preferred,
        selected,
    )
    return selected


def _parse_claim_rewrite(text, expected_count):
    """Accept strict JSON first, then a conservative plain-text fallback."""
    cleaned = _clean_model_text(text)
    paragraph = ""
    bullets = []

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            paragraph = str(parsed.get("paragraph") or "").strip()
            raw_bullets = parsed.get("bullets") or []
            if isinstance(raw_bullets, list):
                bullets = [
                    str(value or "").strip()
                    for value in raw_bullets
                    if str(value or "").strip()
                ]
    except json.JSONDecodeError:
        lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
        bullet_lines = []
        paragraph_lines = []
        for line in lines:
            match = re.match(r"^(?:[-*•]|\d+[.)])\s+(.+)$", line)
            if match:
                bullet_lines.append(match.group(1).strip())
            elif not bullet_lines:
                paragraph_lines.append(re.sub(r"^(?:paragraph|summary)\s*:\s*", "", line, flags=re.I))
        paragraph = " ".join(paragraph_lines).strip()
        bullets = bullet_lines

    if not paragraph or len(bullets) != expected_count:
        return None
    return {"paragraph": paragraph, "bullets": bullets}


def _rewrite_one_claim(base_url, model_id, headers, claim_number, suggestions):
    """Rewrite one claim at a time so the 0.6B local model gets a tiny prompt."""
    expected_count = len(suggestions)
    system_prompt = (
        "/no_think\n"
        "You are a professional copy editor. Do not analyze the claim. Do not add advice. "
        "Rewrite only the supplied approved suggestions. Return JSON only with exactly two keys: "
        "paragraph and bullets. paragraph is one short professional paragraph summarizing the supplied "
        "suggestions. bullets is an array with exactly one rewritten item for each supplied suggestion, "
        "in exactly the same order. Preserve meaning. Do not add, remove, merge, diagnose, infer, or invent "
        "any fact, recommendation, cause, outcome, or claim detail."
    )

    payload = json.dumps({
        "model": model_id,
        "temperature": 0.0,
        "max_tokens": int(os.getenv("MPL_AI_SUGGESTION_MAX_TOKENS", "700")),
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps({
                "claim_number": claim_number,
                "suggestions": suggestions,
                "required_bullet_count": expected_count,
            }, ensure_ascii=False)},
        ],
    }).encode("utf-8")

    outer = _qwen_chat_completion(base_url, payload, headers)
    first_text = outer["choices"][0]["message"]["content"]
    parsed = _parse_claim_rewrite(first_text, expected_count)
    if parsed:
        return parsed

    repair_prompt = (
        "/no_think\n"
        f"Return JSON only: {{\"paragraph\":\"...\",\"bullets\":[...]}}. "
        f"There must be exactly {expected_count} bullet strings, one for each input suggestion in the same order. "
        "Professionally restate the input only. Do not add or remove advice."
    )
    repair_payload = json.dumps({
        "model": model_id,
        "temperature": 0.0,
        "max_tokens": int(os.getenv("MPL_AI_SUGGESTION_MAX_TOKENS", "700")),
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": repair_prompt},
            {"role": "user", "content": json.dumps({"suggestions": suggestions}, ensure_ascii=False)},
        ],
    }).encode("utf-8")
    repaired = _qwen_chat_completion(base_url, repair_payload, headers)
    return _parse_claim_rewrite(repaired["choices"][0]["message"]["content"], expected_count)


def rewrite_python_suggestions(notice):
    """Ask Qwen only to professionally restate Python-generated suggestions."""
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
    headers = {"Content-Type": "application/json"}
    if os.getenv("MPL_AI_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['MPL_AI_API_KEY']}"

    model_id = _discover_live_model(base_url, headers, preferred_model)

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
            if not rewritten:
                raise ValueError("Qwen did not return the required paragraph and bullet count.")
            claims.append({
                "claim_number": claim_number,
                "paragraph": rewritten["paragraph"],
                "bullets": rewritten["bullets"],
            })
        except (HTTPError, URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{claim_number}: {type(exc).__name__}: {exc}")
            logger.warning("MPL Qwen suggestion rewrite failed for %s: %s", claim_number, exc)

    if not claims:
        notice.ai_response = ""
        notice.ai_response_source = ""
        notice.save(update_fields=["ai_suggestions", "ai_response", "ai_response_source", "updated_at"])
        return None

    if failures:
        logger.warning("MPL Qwen suggestion rewrite completed partially: %s", "; ".join(failures))

    notice.ai_response = json.dumps({"claims": claims}, ensure_ascii=False)
    notice.ai_response_source = model_id
    notice.save(update_fields=["ai_suggestions", "ai_response", "ai_response_source", "updated_at"])
    return claims
