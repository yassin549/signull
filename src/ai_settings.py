"""Runtime settings for the OpenRouter-powered Signull AI model.

The dashboard form writes to ``data/openrouter.json`` so the running process
picks up a new API key/model on the very next call without a restart and
without ever mutating the host ``.env``.  When the file has no value we fall
back to the environment.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "data" / "openrouter.json"

DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
DEFAULT_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

# Free-first catalogue surfaced in the dashboard dropdown.  Users can still
# type any OpenRouter model id, including paid ones.  Free slugs come and go —
# refresh from https://openrouter.ai/models?max_price=0 when one retires.
DEFAULT_MODELS: list[str] = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "nex-agi/nex-n2.5-pro:free",
    "thinkingmachines/inkling:free",
    "cohere/north-mini-code:free",
    "openai/gpt-4o-mini",
    "anthropic/claude-3.5-sonnet",
]

_lock = threading.Lock()


def _read_file() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def load_ai_settings() -> dict:
    """Return the effective settings (file overrides env overrides default)."""
    with _lock:
        data = _read_file()
    api_key = str(data.get("api_key") or os.getenv("OPENROUTER_API_KEY") or "").strip()
    model = str(data.get("model") or os.getenv("OPENROUTER_MODEL") or DEFAULT_MODEL).strip()
    base_url = str(data.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
    return {"api_key": api_key, "model": model, "base_url": base_url}


def save_ai_settings(
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> dict:
    """Persist a partial update; ``None`` means "leave unchanged"."""
    with _lock:
        data = _read_file()
        if api_key is not None:
            data["api_key"] = str(api_key).strip()
        if model is not None:
            data["model"] = str(model).strip()
        if base_url is not None:
            data["base_url"] = str(base_url).strip().rstrip("/")
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = SETTINGS_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(SETTINGS_PATH)
    return load_ai_settings()


def mask_api_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "•" * len(key)
    return f"{key[:6]}…{key[-4:]}"


def public_ai_settings() -> dict:
    """Safe-to-serve settings with the key masked."""
    settings = load_ai_settings()
    return {
        "model": settings["model"],
        "base_url": settings["base_url"],
        "api_key_set": bool(settings["api_key"]),
        "api_key_masked": mask_api_key(settings["api_key"]),
        "default_models": list(DEFAULT_MODELS),
    }
