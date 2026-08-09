"""Persist paper bankroll and strategy history across server restarts."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .config import BotConfig

logger = logging.getLogger(__name__)

SESSION_VERSION = 1
SESSION_DIR = Path(__file__).resolve().parent.parent / "data" / "sessions"
MAX_STRATEGY_TRADES = 1000
MAX_EQUITY_HISTORY = 50000


def session_path(config: BotConfig) -> Path:
    return (
        SESSION_DIR
        / f"{config.trading_mode}_{config.asset}_{config.strategy_id}.json"
    )


def _session_identity(config: BotConfig) -> dict[str, str]:
    return {
        "trading_mode": config.trading_mode,
        "asset": config.asset,
        "strategy_id": config.strategy_id,
    }


def load_session(config: BotConfig) -> dict[str, Any] | None:
    path = session_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None

    if int(payload.get("version", 0)) != SESSION_VERSION:
        logger.warning("Ignoring session file with unsupported version: %s", path)
        return None

    for key, expected in _session_identity(config).items():
        if str(payload.get(key)) != expected:
            logger.warning(
                "Ignoring session file %s (%s mismatch)", path, key
            )
            return None

    return payload


def save_session(config: BotConfig, data: dict[str, Any]) -> None:
    path = session_path(config)
    payload = {
        "version": SESSION_VERSION,
        "saved_at": time.time(),
        **_session_identity(config),
        **data,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        logger.exception("Failed to persist bot session to %s", path)


def clear_session(config: BotConfig, initial_capital: float = 100.0) -> dict[str, Any]:
    now = time.time()
    payload = {
        "initial_capital": float(initial_capital),
        "equity": float(initial_capital),
        "peak_equity": float(initial_capital),
        "wins_recent": [],
        "wins_streak": 0,
        "losses_streak": 0,
        "trades_placed": 0,
        "strategy_trades": [],
        "equity_history": [{"t": now, "v": float(initial_capital), "mode": config.trading_mode}],
    }
    save_session(config, payload)
    return payload