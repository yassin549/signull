"""Real-time Polymarket CLOB WebSocket feed."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets

from .config import BotConfig
from .markets import (
    CandleMarket,
    get_current_candle,
    get_next_candle,
    market_to_dict,
)
from .polymarket import PolymarketClient
from .state import BotState

logger = logging.getLogger(__name__)

PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 10
PREFETCH_SEC = 90


class MarketFeed:
    """Streams orderbook + price updates from Polymarket's public market channel."""

    def __init__(self, config: BotConfig, state: BotState):
        self.config = config
        self.state = state
        self._client = PolymarketClient(config)
        self._token_map: dict[str, str] = {}  # token_id -> "up" | "down"
        self._active_slug: str | None = None
        self._active_market: CandleMarket | None = None
        self._next_market: CandleMarket | None = None
        self._switching = False

    async def run(self) -> None:
        while not self.state.should_shutdown():
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Feed reconnecting: %s", exc)
                self.state.set_feed_status(False, {"error": str(exc), "reconnecting": True})
                await asyncio.sleep(0.5)

    async def _connect_and_stream(self) -> None:
        market = await asyncio.to_thread(get_current_candle, self.config.asset)
        if market is None:
            self.state.set_feed_status(False, {"error": "no active market"})
            await asyncio.sleep(1)
            return

        if market.slug != self._active_slug:
            await self._switch_candle(market)

        asset_ids = [market.up_token_id, market.down_token_id]
        self.state.set_feed_status(True, {"subscribed": asset_ids, "reconnecting": False})

        async with websockets.connect(PM_WS_URL, ping_interval=None) as ws:
            await ws.send(json.dumps({
                "assets_ids": asset_ids,
                "type": "market",
                "custom_feature_enabled": True,
            }))

            ping_task = asyncio.create_task(self._ping_loop(ws))
            watch_task = asyncio.create_task(self._candle_watch_loop(ws))
            try:
                async for raw in ws:
                    if self.state.should_shutdown():
                        break
                    await self._handle_message(raw)
            finally:
                for task in (ping_task, watch_task):
                    task.cancel()
                await asyncio.gather(ping_task, watch_task, return_exceptions=True)

    async def _ping_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await ws.send("PING")
            except Exception:
                break

    async def _capture_beat_when_ready(self) -> None:
        for _ in range(24):
            if self.state.should_shutdown():
                return
            await asyncio.sleep(0.25)
            beat = self.state.get_btc_price()
            if beat is not None:
                self.state.set_price_to_beat(beat)
                return

    async def _prefetch_next(self) -> None:
        if self._active_market is None:
            return
        if self._active_market.seconds_to_close > PREFETCH_SEC:
            return
        if (
            self._next_market is not None
            and self._next_market.candle_start_ts > self._active_market.candle_start_ts
        ):
            return

        nxt = await asyncio.to_thread(get_next_candle, self.config.asset, self._active_market)
        if nxt is not None:
            self._next_market = nxt

    async def _candle_watch_loop(self, ws) -> None:
        """Prefetch the next candle, then switch exactly when the current one closes."""
        while not self.state.should_shutdown():
            market = self._active_market
            if market is None:
                await asyncio.sleep(0.25)
                continue

            await self._prefetch_next()

            secs = market.seconds_to_close
            if secs > 0.15:
                await asyncio.sleep(min(secs + 0.05, 0.5))
                continue

            if self._switching:
                await asyncio.sleep(0.1)
                continue

            nxt = self._next_market
            if nxt is None:
                nxt = await asyncio.to_thread(get_next_candle, self.config.asset, market)

            if nxt is None or nxt.slug == self._active_slug:
                await asyncio.sleep(0.1)
                continue

            self._switching = True
            try:
                self._next_market = None
                await self._switch_candle(nxt)
                self.state.set_feed_status(False, {"reconnecting": True})
                await ws.close()
            finally:
                self._switching = False
            return

    async def _switch_candle(self, market: CandleMarket) -> None:
        self._active_slug = market.slug
        self._active_market = market
        self._token_map = {
            market.up_token_id: "up",
            market.down_token_id: "down",
        }
        beat = self.state.get_btc_price()
        self.state.clear_market_data()
        if beat is not None:
            self.state.set_price_to_beat(beat)
        else:
            asyncio.create_task(self._capture_beat_when_ready())
        self.state.update(
            market=market_to_dict(market),
            prices=None,
            signal={"side": "hold", "reason": "New candle — warming up"},
        )
        self.state.log("info", f"New candle: {market.title}")
        await asyncio.gather(
            self._bootstrap_book(market.up_token_id, "up"),
            self._bootstrap_book(market.down_token_id, "down"),
        )

    async def _bootstrap_book(self, token_id: str, side: str) -> None:
        try:
            book = await asyncio.to_thread(self._client.get_order_book, token_id)
            bids = _normalize_levels(getattr(book, "bids", []) or [])
            asks = _normalize_levels(getattr(book, "asks", []) or [])
            if bids or asks:
                self.state.update_feed_book(side, bids, asks)
        except Exception:
            logger.exception("REST book bootstrap failed for %s", side)

    async def _handle_message(self, raw: str | bytes) -> None:
        if raw in ("PONG", "PING"):
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        if isinstance(msg, list):
            for item in msg:
                self._process_event(item)
        else:
            self._process_event(msg)

    def _process_event(self, msg: dict[str, Any]) -> None:
        event_type = msg.get("event_type")
        asset_id = msg.get("asset_id", "")
        side = self._token_map.get(asset_id)

        if event_type == "book" and side:
            bids = _normalize_levels(msg.get("bids", []))
            asks = _normalize_levels(msg.get("asks", []))
            self.state.update_feed_book(side, bids, asks)

        elif event_type == "price_change":
            for change in msg.get("price_changes", []):
                aid = change.get("asset_id", "")
                s = self._token_map.get(aid)
                if not s:
                    continue
                bb = change.get("best_bid")
                ba = change.get("best_ask")
                if bb is not None and ba is not None:
                    self.state.update_feed_best(s, float(bb), float(ba))

        elif event_type == "best_bid_ask" and side:
            self.state.update_feed_best(
                side,
                float(msg.get("best_bid", 0)),
                float(msg.get("best_ask", 1)),
            )

        elif event_type == "last_trade_price" and side:
            self.state.record_trade(
                side,
                float(msg.get("price", 0)),
                float(msg.get("size", 0)),
                msg.get("side", ""),
            )


def _normalize_levels(levels: list) -> list[dict]:
    result = []
    for lvl in levels:
        if isinstance(lvl, dict):
            price = float(lvl.get("price", 0))
            size = float(lvl.get("size", 0))
        else:
            price = float(getattr(lvl, "price", 0))
            size = float(getattr(lvl, "size", 0))
        if size > 0:
            result.append({"price": price, "size": size})
    return result