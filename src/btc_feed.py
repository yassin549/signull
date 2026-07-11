"""Polymarket RTDS spot price streams (Binance fast + Chainlink oracle)."""

from __future__ import annotations

import asyncio
import json
import logging

import websockets

from .state import BotState

logger = logging.getLogger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"
PING_INTERVAL = 5

# Binance RTDS symbol → asset key; Chainlink filter symbol per asset
BINANCE_SYMBOLS = {
    "btc": "btcusdt",
    "eth": "ethusdt",
    "sol": "solusdt",
    "xrp": "xrpusdt",
}
CHAINLINK_SYMBOLS = {
    "btc": "btc/usd",
    "eth": "eth/usd",
    "sol": "sol/usd",
    "xrp": "xrp/usd",
}


class BtcPriceFeed:
    """Binance for fast display; Chainlink for resolution delta vs beat.

    Named historically for BTC; supports eth/sol/xrp via *asset*.
    """

    def __init__(self, state: BotState, asset: str = "btc"):
        self.state = state
        self.asset = (asset or "btc").lower()
        if self.asset not in BINANCE_SYMBOLS:
            raise ValueError(
                f"Unsupported asset for price feed: {asset!r}; "
                f"supported: {sorted(BINANCE_SYMBOLS)}"
            )
        self._binance_sym = BINANCE_SYMBOLS[self.asset]
        self._chainlink_sym = CHAINLINK_SYMBOLS[self.asset]

    async def run(self) -> None:
        while not self.state.should_shutdown():
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("%s feed reconnecting: %s", self.asset.upper(), exc)
                self.state.set_btc_feed_status(False, str(exc))
                await asyncio.sleep(0.5)

    async def _connect_and_stream(self) -> None:
        async with websockets.connect(RTDS_URL, ping_interval=None) as ws:
            await ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [
                    # Broad crypto_prices stream; filter symbol in-process.
                    {"topic": "crypto_prices", "type": "update"},
                    {
                        "topic": "crypto_prices_chainlink",
                        "type": "*",
                        "filters": json.dumps({"symbol": self._chainlink_sym}),
                    },
                ],
            }))
            self.state.set_btc_feed_status(True)
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    if self.state.should_shutdown():
                        break
                    self._handle_message(raw)
            finally:
                ping_task.cancel()
                await asyncio.gather(ping_task, return_exceptions=True)

    async def _ping_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await ws.send("PING")
            except Exception:
                break

    def _handle_message(self, raw: str | bytes) -> None:
        if raw in ("PONG", "PING"):
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Some RTDS envelopes nest payload; tolerate both shapes.
        if isinstance(msg, list):
            for item in msg:
                if isinstance(item, dict):
                    self._dispatch(item)
            return
        if isinstance(msg, dict):
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        topic = msg.get("topic")
        payload = msg.get("payload") or {}
        msg_type = msg.get("type")

        if topic == "crypto_prices":
            if msg_type in ("update", "*", None) or msg_type == "subscribe":
                if isinstance(payload.get("data"), list):
                    for point in payload["data"]:
                        if self._binance_point_matches(point):
                            self._ingest_binance(point)
                elif payload.get("value") is not None and self._binance_point_matches(payload):
                    self._ingest_binance(payload)
            return

        if topic != "crypto_prices_chainlink":
            return

        if msg_type == "subscribe" and isinstance(payload.get("data"), list):
            for point in payload["data"]:
                if self._chainlink_point_matches(point):
                    self._ingest_chainlink(point)
            return

        if payload.get("value") is not None and self._chainlink_point_matches(payload):
            self._ingest_chainlink(payload)

    def _binance_point_matches(self, point: dict) -> bool:
        if not isinstance(point, dict):
            return False
        raw = (point.get("symbol") or "").lower()
        if not raw:
            # Some RTDS updates omit symbol after subscribe; accept value ticks
            # for our single-asset process filter.
            return point.get("value") is not None
        sym = raw.replace("-", "").replace("/", "")
        want = self._binance_sym.replace("/", "")
        return sym == want or sym == f"{self.asset}usd" or sym == f"{self.asset}usdt"

    def _chainlink_point_matches(self, point: dict) -> bool:
        if not isinstance(point, dict):
            return False
        sym = (point.get("symbol") or "").lower()
        # Some payloads omit symbol when subscription is already filtered
        return not sym or sym == self._chainlink_sym

    def _ingest_binance(self, point: dict) -> None:
        value = point.get("value")
        ts = point.get("timestamp")
        if value is None:
            return
        # Fall back to wall clock if RTDS omits timestamp
        if ts is None:
            import time
            ts = int(time.time() * 1000)
        self.state.update_btc_price(float(value), int(ts))

    def _ingest_chainlink(self, point: dict) -> None:
        value = point.get("value")
        ts = point.get("timestamp")
        if value is None:
            return
        if ts is None:
            import time
            ts = int(time.time() * 1000)
        self.state.update_btc_chainlink(float(value), int(ts))
