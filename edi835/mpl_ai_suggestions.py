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

DEFAULT_QWEN_BASE_URL = "http://127.0.0.1:8080/v1"


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
        # Small local models occasionally ignore response_format. Accept only a
        # very simple paragraph + numbered/bulleted list shape; never infer new
        # content from malformed output.
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

    # One short repair attempt is much more reliable on the small Qwen model
    # than rejecting the whole notice because of a formatting mistake.
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

    # The Qwen server is a local service on production. Keep the environment
    # override, but use the deployed local URL by default so a missing optional
    # worker env file does not silently disable AI suggestions.
    base_url = os.getenv("MPL_AI_BASE_URL", DEFAULT_QWEN_BASE_URL).rstrip("/")
    model_id = os.getenv("MPL_AI_MODEL", "qwen3-0.6b-instruct-q4_k_m")
    headers = {"Content-Type": "application/json"}
    if os.getenv("MPL_AI_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['MPL_AI_API_KEY']}"

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
