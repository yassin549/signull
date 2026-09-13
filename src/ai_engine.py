"""Conversation + decision engine for the Signull AI model.

The engine keeps one growing chat transcript so the model only receives the
full market briefing once.  Every later candle appends a compact "what just
happened + new candle state" update, which keeps token usage (and therefore
cost) low while preserving the model's chain of thought.

Only the most recent ``max_messages`` turns are actually sent to OpenRouter;
the full transcript is retained locally for the dashboard monitor.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ai_settings import load_ai_settings
from .openrouter import OpenRouterClient, OpenRouterError

logger = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "ai_conversation.json"

# Bump when SYSTEM_PROMPT changes so an old transcript is discarded.
PROMPT_VERSION = 2

SYSTEM_PROMPT = """You are Signull AI, a disciplined 5-minute Bitcoin up/down trading model.
Each round you receive ONLY the last 5 completed BTC 5-minute candles as OHLCV and must
predict whether the NEXT 5-minute candle will close UP (above its open) or DOWN (below
its open).

Rules:
- Always choose exactly one side: "up" or "down". Never hold.
- Reason purely from the raw price action in the 5 candles: candle bodies, wicks,
  sequences, momentum, range and volume. Do not invent indicators or outside data.
- Calibrate confidence honestly: it is your probability (0.50-1.00) that the next
  candle closes on the side you chose. Low-conviction setups should stay near 0.50.

Reply with a single JSON object and nothing else:
{"decision": "up" | "down", "confidence": 0.0-1.0, "reasoning": "2-4 sentences", "key_factors": ["...", "..."]}
"""


@dataclass
class Decision:
    side: str
    confidence: float
    reasoning: str
    key_factors: list[str]
    raw: str
    model: str
    usage: dict[str, Any]
    latency_ms: float
    context_text: str = ""
    ts: float = field(default_factory=time.time)

    def to_public(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "confidence": round(self.confidence, 4),
            "reasoning": self.reasoning,
            "key_factors": list(self.key_factors),
            "model": self.model,
            "latency_ms": round(self.latency_ms, 1),
            "ts": self.ts,
        }


def _first_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start : idx + 1]
                    try:
                        parsed = json.loads(chunk)
                    except ValueError:
                        break
                    return parsed if isinstance(parsed, dict) else None
        start = text.find("{", start + 1)
    return None


def parse_decision(content: str) -> tuple[str, float, str, list[str]]:
    """Extract side/confidence/reasoning/factors from a model reply."""
    text = (content or "").strip()
    if not text:
        raise ValueError("Empty model response")

    stripped = text.strip("`").strip()
    data = _first_json_object(stripped)

    side = ""
    confidence: float | None = None
    reasoning = ""
    factors: list[str] = []

    if data is not None:
        side = str(data.get("decision") or data.get("side") or "").strip().lower()
        raw_conf = data.get("confidence", data.get("prob", data.get("probability")))
        if isinstance(raw_conf, (int, float)):
            confidence = float(raw_conf)
        elif isinstance(raw_conf, str):
            try:
                confidence = float(raw_conf.strip().rstrip("%"))
            except ValueError:
                confidence = None
        reasoning = str(data.get("reasoning") or data.get("reason") or data.get("analysis") or "").strip()
        raw_factors = data.get("key_factors") or data.get("factors") or []
        if isinstance(raw_factors, list):
            factors = [str(f).strip() for f in raw_factors if str(f).strip()]

    if side not in ("up", "down"):
        match = re.search(r'"?\b(?:decision|side)\b"?\s*[:=]\s*"?\s*(up|down)', text, re.IGNORECASE)
        if match:
            side = match.group(1).lower()
    if side not in ("up", "down"):
        # Conservative prose fallback: only trust an up/down word in the closing
        # lines when it sits next to an explicit decision cue.  A truncated
        # reasoning model must not have its side guessed from arbitrary words.
        tail = text[-400:]
        if re.search(r"(decision|choose|predict|final|call|go|close)", tail, re.IGNORECASE):
            words = re.findall(r"\b(up|down)\b", tail, re.IGNORECASE)
            if words:
                side = words[-1].lower()
    if side not in ("up", "down"):
        raise ValueError(f"Could not parse up/down decision from: {text[:200]}")

    if confidence is None:
        confidence = 0.6
    if confidence > 1.0:
        confidence = confidence / 100.0
    confidence = max(0.5, min(1.0, float(confidence)))

    if not reasoning:
        reasoning = text[:400]

    return side, confidence, reasoning, factors


class AIDecisionEngine:
    def __init__(
        self,
        *,
        client: OpenRouterClient | None = None,
        max_messages: int = 24,
        max_transcript: int = 400,
        persist: bool = True,
    ):
        self._client = client or OpenRouterClient()
        self._max_messages = int(max_messages)
        self._max_transcript = int(max_transcript)
        self._persist = persist
        self._lock = threading.Lock()
        self._version = 0
        self._messages: list[dict[str, Any]] = []
        self._usage: dict[str, int] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self._status = "idle"
        self._last_error: str | None = None
        self._last_model: str | None = None
        self._loaded = False
        self._load()

    # ── lifecycle ──────────────────────────────────────────────────────────
    def _bump(self) -> None:
        self._version += 1

    def reset(self) -> None:
        with self._lock:
            self._messages = [{"role": "system", "content": SYSTEM_PROMPT, "ts": time.time()}]
            self._usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            self._status = "idle"
            self._last_error = None
            self._bump()
        self._save()

    def _load(self) -> None:
        if not self._persist:
            self.reset()
            self._loaded = True
            return
        try:
            payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("prompt_version") == PROMPT_VERSION and isinstance(payload.get("messages"), list) and payload["messages"]:
            with self._lock:
                self._messages = [
                    m for m in payload["messages"] if isinstance(m, dict) and m.get("role") and "content" in m
                ]
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    for key in self._usage:
                        value = usage.get(key)
                        if isinstance(value, (int, float)):
                            self._usage[key] = int(value)
                self._last_model = payload.get("last_model") or None
                self._bump()
        else:
            with self._lock:
                self._messages = [{"role": "system", "content": SYSTEM_PROMPT, "ts": time.time()}]
                self._bump()
        self._loaded = True

    def _save(self) -> None:
        if not self._persist:
            return
        try:
            with self._lock:
                payload = {
                    "saved_at": time.time(),
                    "prompt_version": PROMPT_VERSION,
                    "messages": list(self._messages[-self._max_transcript :]),
                    "usage": dict(self._usage),
                    "last_model": self._last_model,
                }
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = STATE_PATH.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(STATE_PATH)
        except OSError:
            logger.debug("Failed to persist AI conversation", exc_info=True)

    # ── decisions ──────────────────────────────────────────────────────────
    def _api_messages_locked(self) -> list[dict[str, str]]:
        msgs = self._messages
        if len(msgs) <= self._max_messages:
            return [{"role": m["role"], "content": m["content"]} for m in msgs]
        tail = msgs[-(self._max_messages - 1) :]
        return [{"role": msgs[0]["role"], "content": msgs[0]["content"]}] + [
            {"role": m["role"], "content": m["content"]} for m in tail
        ]

    def decide(
        self,
        context_text: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 700,
    ) -> Decision:
        with self._lock:
            self._status = "thinking"
            self._last_error = None
            self._messages.append({"role": "user", "content": context_text, "ts": time.time()})
            self._bump()
            api_messages = self._api_messages_locked()

        try:
            response = self._client.complete(
                api_messages, temperature=temperature, max_tokens=max_tokens
            )
            side, confidence, reasoning, factors = parse_decision(response.get("content", ""))
        except (OpenRouterError, ValueError) as exc:
            with self._lock:
                # Drop the unanswered user turn so the transcript stays valid.
                if self._messages and self._messages[-1]["role"] == "user":
                    self._messages.pop()
                self._status = "error"
                self._last_error = str(exc)
                self._bump()
            self._save()
            raise

        usage = response.get("usage") or {}
        with self._lock:
            self._messages.append(
                {"role": "assistant", "content": response.get("content", ""), "ts": time.time()}
            )
            if len(self._messages) > self._max_transcript:
                self._messages = [self._messages[0]] + self._messages[-(self._max_transcript - 1) :]
            self._usage["calls"] += 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(key)
                if isinstance(value, (int, float)):
                    self._usage[key] += int(value)
            self._status = "ok"
            self._last_model = response.get("model")
            self._bump()

        decision = Decision(
            side=side,
            confidence=confidence,
            reasoning=reasoning,
            key_factors=factors,
            raw=response.get("content", ""),
            model=response.get("model", ""),
            usage=usage,
            latency_ms=float(response.get("latency_ms", 0.0)),
            context_text=context_text,
        )
        self._save()
        return decision

    # ── introspection ──────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        settings = load_ai_settings()
        with self._lock:
            messages = [
                {
                    "role": m.get("role", "?"),
                    "content": str(m.get("content", ""))[:3000],
                    "ts": m.get("ts"),
                }
                for m in self._messages[-40:]
            ]
            return {
                "status": self._status,
                "last_error": self._last_error,
                "model": self._last_model or settings["model"],
                "api_key_set": bool(settings["api_key"]),
                "usage": dict(self._usage),
                "messages": messages,
                "version": self._version,
            }
