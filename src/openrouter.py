"""Minimal OpenRouter (OpenAI-compatible) chat client.

Uses ``httpx`` which is already a project dependency, so no extra package is
required.  Settings are re-read on every call so a key/model change made from
the dashboard takes effect immediately.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .ai_settings import load_ai_settings

logger = logging.getLogger(__name__)


class OpenRouterError(RuntimeError):
    """Raised when OpenRouter is unreachable or returns a non-2xx response."""


class OpenRouterClient:
    def __init__(self, timeout: float = 30.0):
        self._timeout = float(timeout)

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1500,
    ) -> dict[str, Any]:
        """Send a chat completion and return content + usage + latency."""
        settings = load_ai_settings()
        api_key = settings["api_key"]
        if not api_key:
            raise OpenRouterError("No OpenRouter API key configured")

        use_model = (model or settings["model"]).strip()
        if not use_model:
            raise OpenRouterError("No OpenRouter model configured")

        base_url = settings["base_url"]
        payload = {
            "model": use_model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://signull.local",
            "X-Title": "Signull AI",
        }

        started = time.time()
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

        latency_ms = (time.time() - started) * 1000.0
        if resp.status_code >= 400:
            detail = (resp.text or "").strip()[:300]
            raise OpenRouterError(f"OpenRouter HTTP {resp.status_code}: {detail}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise OpenRouterError("OpenRouter returned invalid JSON") from exc

        choices = data.get("choices") or []
        content = ""
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
        usage = data.get("usage") or {}
        return {
            "content": str(content),
            "model": str(data.get("model") or use_model),
            "usage": usage,
            "latency_ms": latency_ms,
        }


def _normalize_model(raw: dict[str, Any]) -> dict[str, Any]:
    """Reduce an OpenRouter model object to the fields the dashboard needs."""
    pricing = raw.get("pricing") or {}

    def _price(key: str) -> float | None:
        try:
            return float(pricing.get(key))
        except (TypeError, ValueError):
            return None

    prompt = _price("prompt")
    completion = _price("completion")
    free = prompt == 0 and completion == 0
    return {
        "id": raw.get("id") or "",
        "name": raw.get("name") or raw.get("id") or "",
        "context_length": raw.get("context_length"),
        "free": bool(free),
        "prompt_price": prompt,
        "completion_price": completion,
    }


def list_models(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """List models available to the configured OpenRouter key.

    Prefers the account-scoped ``/models/user`` endpoint (respects the key's
    privacy/data-policy filters) and falls back to the public catalogue.
    """
    settings = load_ai_settings()
    key = (api_key or settings["api_key"] or "").strip()
    base = (base_url or settings["base_url"]).strip().rstrip("/")
    headers = {"User-Agent": "signull"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    last_error: str | None = None
    for path in ("/models/user", "/models"):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(f"{base}{path}", headers=headers)
        except httpx.HTTPError as exc:
            last_error = f"OpenRouter request failed: {exc}"
            continue
        if resp.status_code >= 400:
            last_error = f"OpenRouter HTTP {resp.status_code} on {path}"
            continue
        try:
            payload = resp.json()
        except ValueError:
            last_error = "OpenRouter returned invalid JSON"
            continue
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            last_error = "Unexpected OpenRouter model list payload"
            continue
        models = [_normalize_model(m) for m in rows if isinstance(m, dict) and m.get("id")]
        if models:
            models.sort(key=lambda m: (not m["free"], str(m["name"]).lower()))
            return models

    raise OpenRouterError(last_error or "Could not load OpenRouter models")
