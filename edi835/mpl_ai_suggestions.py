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
SERVER_ENV_PATH = "/etc/mpl-ai-server.env"


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


def _read_env_value(path, key):
    """Read one simple KEY=value setting without executing an env file."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() != key:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                return value.strip()
    except (OSError, UnicodeError):
        return ""
    return ""


def _qwen_api_key(base_url):
    """Prefer the credential used by the local llama.cpp server when readable."""
    configured = str(os.getenv("MPL_AI_API_KEY") or "").strip()
    local_server = base_url.startswith("http://127.0.0.1:") or base_url.startswith("http://localhost:")
    if local_server:
        server_key = _read_env_value(SERVER_ENV_PATH, "MPL_AI_API_KEY")
        if server_key:
            if configured and configured != server_key:
                logger.warning("MPL worker Qwen key differed from local server key; using the server credential.")
            return server_key
    return configured


def _qwen_headers(base_url):
    headers = {"Content-Type": "application/json"}
    api_key = _qwen_api_key(base_url)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _discover_live_model(base_url, headers, preferred):
    """Use the model actually exposed by the local Qwen server."""
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
    """Backward-compatible parser used by tests and legacy model output."""
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


def _strip_single_answer(value):
    """Normalize one short Qwen response without requiring JSON formatting."""
    text = _clean_model_text(value)
    text = re.sub(r"^(?:answer|response|paragraph|rewrite)\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return re.sub(r"\s+", " ", text).strip()


def _qwen_text(base_url, model_id, headers, system_prompt, user_payload, max_tokens):
    """Call Qwen for a plain-text answer; no response_format contract is required."""
    payload = json.dumps({
        "model": model_id,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ],
    }).encode("utf-8")
    outer = _qwen_chat_completion(base_url, payload, headers)
    text = _strip_single_answer(outer["choices"][0]["message"]["content"])
    if not text:
        raise ValueError("Qwen returned an empty response.")
    return text


def _rewrite_one_claim(base_url, model_id, headers, claim_number, suggestions):
    """Use tiny independent Qwen prompts so the 0.6B model is reliable.

    One call creates the professional paragraph. Each approved Python suggestion
    is then rewritten independently, guaranteeing one AI bullet per input action
    without asking the small model to obey a brittle JSON/counting contract.
    """
    paragraph_prompt = (
        "/no_think\n"
        "You are a professional copy editor. Rewrite only the supplied approved recommendations "
        "as one concise professional paragraph. Preserve every recommendation's meaning. "
        "Do not add facts, advice, causes, outcomes, diagnoses, or claim details. "
        "Return only the paragraph, with no heading and no bullets."
    )
    paragraph = _qwen_text(
        base_url,
        model_id,
        headers,
        paragraph_prompt,
        json.dumps({"approved_recommendations": suggestions}, ensure_ascii=False),
        int(os.getenv("MPL_AI_PARAGRAPH_MAX_TOKENS", "220")),
    )

    bullet_prompt = (
        "/no_think\n"
        "You are a professional copy editor. Professionally restate the supplied approved recommendation "
        "as one concise sentence. Preserve its meaning exactly. Do not add facts, advice, causes, outcomes, "
        "diagnoses, or claim details. Return only the rewritten sentence."
    )
    bullets = []
    for suggestion in suggestions:
        bullets.append(_qwen_text(
            base_url,
            model_id,
            headers,
            bullet_prompt,
            suggestion,
            int(os.getenv("MPL_AI_BULLET_MAX_TOKENS", "120")),
        ))

    return {"paragraph": paragraph, "bullets": bullets}


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
    headers = _qwen_headers(base_url)
    model_id = _discover_live_model(base_url, headers, preferred_model)

    logger.info(
        "MPL Qwen suggestion rewrite starting: notice=%s model=%s groups=%s",
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
                "MPL Qwen suggestion rewrite completed for claim %s with %s bullet(s).",
                claim_number,
                len(rewritten["bullets"]),
            )
        except (HTTPError, URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{claim_number}: {type(exc).__name__}: {exc}")
            logger.exception("MPL Qwen suggestion rewrite failed for %s", claim_number)

    if not claims:
        notice.ai_response = ""
        notice.ai_response_source = ""
        notice.save(update_fields=["ai_suggestions", "ai_response", "ai_response_source", "updated_at"])
        logger.error(
            "MPL Qwen suggestion rewrite produced no claim output for notice %s: %s",
            notice.pk,
            "; ".join(failures) or "no groups",
        )
        return None

    if failures:
        logger.warning("MPL Qwen suggestion rewrite completed partially: %s", "; ".join(failures))

    notice.ai_response = json.dumps({"claims": claims}, ensure_ascii=False)
    notice.ai_response_source = model_id
    notice.save(update_fields=["ai_suggestions", "ai_response", "ai_response_source", "updated_at"])
    return claims
