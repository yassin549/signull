"""Rest-only Binance price feed (no WebSocket)."""

from __future__ import annotations

import asyncio
import logging
import time

import requests

from .config import PREDICT_API_HOST
from .state import BotState

logger = logging.getLogger(__name__)

POLL_INTERVAL = 1.0
BINANCE_REST_SYMBOLS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}


class BtcPriceFeed:
    def __init__(self, state: BotState, asset: str = "btc"):
        self.state = state
        self.asset = (asset or "btc").lower()
        if self.asset not in BINANCE_REST_SYMBOLS:
            raise ValueError(f"Unsupported asset: {asset}")
        self._symbol = BINANCE_REST_SYMBOLS[self.asset]

    async def run(self) -> None:
        while not self.state.should_shutdown():
            try:
                resp = await asyncio.to_thread(
                    requests.get,
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": self._symbol},
                    timeout=4,
                )
                if resp.status_code == 200:
                    price = float(resp.json()["price"])
                    await asyncio.to_thread(
                        self.state.update_btc_price, price, int(time.time() * 1000)
                    )
                    self.state.set_btc_feed_status(True)
            except Exception as exc:
                self.state.set_btc_feed_status(False, str(exc))
            await asyncio.sleep(POLL_INTERVAL)


def fetch_candle_open_price(asset: str, candle_start_ts: int) -> float | None:
    asset_key = (asset or "btc").lower()
    sym = BINANCE_REST_SYMBOLS.get(asset_key, "BTCUSDT")
    start_ms = int(candle_start_ts) * 1000
    for attempt in range(2):
        try:
            resp = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": sym, "interval": "5m", "startTime": start_ms, "limit": 1},
                timeout=6,
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0 and len(data[0]) > 1:
                    return float(data[0][1])
        except Exception as exc:
            if attempt == 0:
                time.sleep(0.3)
                continue
            logger.warning("Failed to fetch candle open price for %s at %s: %s", asset, candle_start_ts, exc)
    return None


def fetch_candle_history(
    asset: str, candle_start_ts: int, end_ts: float | None = None,
) -> list[dict[str, float]]:
    asset_key = (asset or "btc").lower()
    sym = BINANCE_REST_SYMBOLS.get(asset_key, "BTCUSDT")
    start_ms = int(candle_start_ts) * 1000
    now_ms = int((end_ts if end_ts is not None else time.time()) * 1000)
    if now_ms <= start_ms:
        return []

    for attempt in range(2):
        try:
            resp = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={
                    "symbol": sym, "interval": "1s",
                    "startTime": start_ms, "endTime": now_ms, "limit": 500,
                },
                timeout=6,
            )
            if resp.status_code != 200:
                if attempt == 0:
                    time.sleep(0.3)
                    continue
                return []
            data = resp.json()
            points: list[dict[str, float]] = []
            if isinstance(data, list):
                for k in data:
                    if len(k) > 1:
                        t_sec = float(k[0]) / 1000.0
                        open_px = float(k[1])
                        points.append({"t": t_sec, "v": round(open_px, 2)})
            return points
        except Exception as exc:
            if attempt == 0:
                time.sleep(0.3)
                continue
            logger.warning("Failed to fetch candle history for %s: %s", asset, exc)
            return []
    return []


def fetch_outcome_price_history(
    up_token_id: str, down_token_id: str, candle_start_ts: int, end_ts: float | None = None,
) -> list[dict]:
    return []