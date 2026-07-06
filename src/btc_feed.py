"""Polymarket RTDS BTC price streams (Binance fast + Chainlink oracle)."""

from __future__ import annotations

import asyncio
import json
import logging

import websockets

from .state import BotState

logger = logging.getLogger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"
PING_INTERVAL = 5


class BtcPriceFeed:
    """Binance for fast display; Chainlink for resolution delta vs beat."""

    def __init__(self, state: BotState):
        self.state = state

    async def run(self) -> None:
        while not self.state.should_shutdown():
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("BTC feed reconnecting: %s", exc)
                self.state.set_btc_feed_status(False, str(exc))
                await asyncio.sleep(0.5)

    async def _connect_and_stream(self) -> None:
        async with websockets.connect(RTDS_URL, ping_interval=None) as ws:
            await ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [
                    # No symbol filter — Polymarket RTDS ignores btcusdt filter; we filter in code.
                    {"topic": "crypto_prices", "type": "update"},
                    {
                        "topic": "crypto_prices_chainlink",
                        "type": "*",
                        "filters": '{"symbol":"btc/usd"}',
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

        topic = msg.get("topic")
        payload = msg.get("payload") or {}
        msg_type = msg.get("type")

        if topic == "crypto_prices":
            if msg_type == "update" and payload.get("value") is not None:
                if (payload.get("symbol") or "").lower() == "btcusdt":
                    self._ingest_binance(payload)
            return

        if topic != "crypto_prices_chainlink":
            return

        if msg_type == "subscribe" and isinstance(payload.get("data"), list):
            for point in payload["data"]:
                self._ingest_chainlink(point)
            return

        if msg_type == "update" and payload.get("value") is not None:
            self._ingest_chainlink(payload)

    def _ingest_binance(self, point: dict) -> None:
        value = point.get("value")
        ts = point.get("timestamp")
        if value is None or ts is None:
            return
        self.state.update_btc_price(float(value), int(ts))

    def _ingest_chainlink(self, point: dict) -> None:
        value = point.get("value")
        ts = point.get("timestamp")
        if value is None or ts is None:
            return
        self.state.update_btc_chainlink(float(value), int(ts))