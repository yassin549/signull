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
        max_tokens: int = 700,
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
