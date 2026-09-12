#!/usr/bin/env python3
"""Shared OpenRouter chat client for the AnomalyGuessr pipeline (ticket #1372).

Three of the generator's model calls are plain chat calls over an image: the
anomaly proposal, the coordinate request and the checker. They share one
request shape, one JSON extractor and one usage roll-up, so the prompts and
the plumbing cannot drift apart.

The client is deliberately thin (stdlib urllib): the pipeline runs inside a
cron whose PATH and env are small, and the repository already speaks
OpenRouter directly in `ag_generate.py` and `ag_verify.py`.
"""

import base64
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

MIME_BY_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
}


class LLMError(RuntimeError):
    """One chat call failed (transport, HTTP error or unusable answer)."""


def data_url(path: Path) -> str:
    """Local image as a data URL (the shape OpenRouter accepts)."""
    ext = Path(path).suffix.lower().lstrip(".")
    mime = MIME_BY_EXT.get(ext, "image/jpeg")
    return f"data:{mime};base64,{base64.b64encode(Path(path).read_bytes()).decode()}"


def decode_data_url(url: str) -> bytes:
    if not url.startswith("data:"):
        raise LLMError(f"unexpected image url (not a data URL): {url[:40]}")
    _, _, b64 = url.partition(",")
    return base64.b64decode(b64)


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(image: Path) -> dict:
    return {"type": "image_url", "image_url": {"url": data_url(image)}}


def chat(messages: list, api_key: str, model: str,
         base_url: str = DEFAULT_BASE_URL, max_tokens: int | None = None,
         temperature: float | None = None, timeout: int = 120,
         reasoning: bool = False, seed: int | None = None) -> dict:
    """One OpenRouter chat completion; returns the raw response body.

    ``reasoning=False`` sends ``reasoning: {enabled: false}``: with thinking
    on, deepseek-v4.1-flash spends the whole output budget on
    ``reasoning_content`` and returns ``content: null`` even at 1200 tokens
    (measured 2026-09-11, ticket #1211).
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "reasoning": {"enabled": bool(reasoning)},
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if temperature is not None:
        payload["temperature"] = temperature
    if seed is not None:
        payload["seed"] = seed
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise LLMError(f"chat request failed: HTTP {e.code} "
                       f"{e.read()[:300]!r}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"chat request failed: {e}") from e


def content_of(body: dict) -> str:
    """The assistant text of a response, "" when there is none."""
    choices = body.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        # Some providers return content parts; join the text ones.
        return "".join(str(p.get("text") or "") for p in content
                       if isinstance(p, dict))
    return str(content or "")


def reasoning_of(body: dict) -> str:
    """The hidden chain-of-thought of a response, "" when there is none.

    DeepSeek and similar thinking models put it in ``reasoning_content``;
    a few providers name it ``reasoning`` (same fallback as ag_verify).
    """
    choices = body.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return str(msg.get("reasoning_content") or msg.get("reasoning") or "")


def usage_of(body: dict) -> dict:
    """Token/cost footprint of one response, zeros when absent.

    ``reasoning_tokens`` is the share of the completion spent on hidden CoT
    (OpenRouter reports it under ``completion_tokens_details``); the trace
    records it so a scene that burned its whole budget thinking is visible.
    """
    u = body.get("usage") or {}
    details = u.get("completion_tokens_details") or {}
    return {"prompt_tokens": int(u.get("prompt_tokens") or 0),
            "completion_tokens": int(u.get("completion_tokens") or 0),
            "reasoning_tokens": int(details.get("reasoning_tokens") or 0),
            "cost": float(u.get("cost") or 0.0)}


def add_usage(total: dict, usage) -> dict:
    """Fold one response's usage into a running total."""
    usage = usage or {}
    total["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
    total["completion_tokens"] += int(usage.get("completion_tokens") or 0)
    total["reasoning_tokens"] = (total.get("reasoning_tokens", 0)
                                 + int(usage.get("reasoning_tokens") or 0))
    total["cost"] += float(usage.get("cost") or 0.0)
    return total


def zero_usage() -> dict:
    return {"prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "cost": 0.0}


def strip_fences(text: str) -> str:
    s = str(text or "").strip()
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", s).strip()


def parse_json(content) -> dict | None:
    """The first JSON object in a model answer, None when there is none.

    Lenient about markdown fences and surrounding prose (models add both),
    strict about the payload: a non-object answer is unusable.
    """
    s = strip_fences(content)
    if not s:
        return None
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def clean_text(value, limit: int = 400) -> str:
    """A model-provided string, whitespace-collapsed and length-capped."""
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return s[:limit]
