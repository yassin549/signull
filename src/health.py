"""Process + upstream health published on the dashboard snapshot."""

from __future__ import annotations

import logging
import time
from typing import Any

from .config import BotConfig
from .state import BotState

logger = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(self, config: BotConfig, state: BotState, *, started_at: float | None = None):
        self.config = config
        self.state = state
        self.started_at = started_at if started_at is not None else time.time()
        self._loop_lag_ms = 0.0
        self._bot_alive = False
        self._last_publish = 0.0

    def set_bot_alive(self, alive: bool) -> None:
        self._bot_alive = bool(alive)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        snap = self.state.get_snapshot(history_points=0)
        feed = snap.get("feed") or {}
        btc = snap.get("btc") or {}
        account = snap.get("account") or {}
        wallet_health = (snap.get("health") or {}).get("wallet") or {}
        rpc_health = (snap.get("health") or {}).get("rpc") or {}
        bot_strategy = snap.get("strategy") or {}

        feed_age = _age(now, feed.get("last_update_at"))
        spot_age = _age(now, btc.get("updated_at"))
        wallet_age = _age(now, account.get("updated_at") or wallet_health.get("updated_at"))

        # Heartbeat health from bot
        hb_failures = bot_strategy.get("heartbeat_failures", 0)
        hb_last_ok = bot_strategy.get("heartbeat_last_ok")
        hb_age = _age(now, hb_last_ok) if hb_last_ok else None

        return {
            "uptime_sec": round(now - self.started_at, 1),
            "loop_lag_ms": round(self._loop_lag_ms, 2),
            "bot_alive": self._bot_alive,
            "updated_at": now,
            "feed": {
                "connected": bool(feed.get("connected")),
                "reconnecting": bool(feed.get("reconnecting")),
                "updates_per_sec": feed.get("updates_per_sec"),
                "age_sec": feed_age,
                "error": feed.get("error"),
            },
            "spot": {
                "connected": bool(btc.get("connected")),
                "age_sec": spot_age,
                "error": btc.get("error"),
            },
            "wallet": {
                "connected": bool(account.get("connected") or wallet_health.get("ok")),
                "age_sec": wallet_age,
                "error": wallet_health.get("error") or account.get("error"),
            },
            "rpc": {
                "ok": bool(rpc_health.get("ok")),
                "age_sec": _age(now, rpc_health.get("updated_at")),
                "block": rpc_health.get("block"),
                "rtt_ms": rpc_health.get("rtt_ms"),
                "error": rpc_health.get("error"),
            },
            "heartbeat": {
                "failures": hb_failures,
                "age_sec": hb_age,
                "last_ok": hb_last_ok,
            },
        }

    def publish(self) -> None:
        health = dict(self.state.get_snapshot(history_points=0).get("health") or {})
        health.update(self.snapshot())
        self.state.update(health=health)
        self._last_publish = time.time()

    async def run(self) -> None:
        while not self.state.should_shutdown():
            t0 = time.perf_counter()
            # Yield to measure event-loop lag; a healthy loop wakes immediately.
            import asyncio

            await asyncio.sleep(0)
            self._loop_lag_ms = (time.perf_counter() - t0) * 1000.0
            try:
                self.publish()
            except Exception:
                logger.debug("health publish failed", exc_info=True)
            await asyncio.sleep(1.0)


def _age(now: float, ts: object) -> float | None:
    try:
        value = float(ts)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return max(0.0, round(now - value, 2))
