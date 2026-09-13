"""Real-time Predict.fun orderbook polling feed."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .config import BotConfig
from .markets import (
    CANDLE_DURATION,
    CandleMarket,
    expected_candle_start_ts,
    get_current_candle,
    get_next_candle,
    market_to_dict,
    provisional_market_dict,
)
from .predict_client import PredictClient
from .state import BotState

logger = logging.getLogger(__name__)

PREFETCH_SEC = 120
ORDERBOOK_POLL_SEC = 1.5


class MarketFeed:
    def __init__(self, config: BotConfig, state: BotState):
        self.config = config
        self.state = state
        self._client = PredictClient(config)
        self._active_slug: str | None = None
        self._active_market: CandleMarket | None = None
        self._next_market: CandleMarket | None = None
        self._switching = False
        self._provisional_pushed_for: int | None = None

    async def close(self) -> None:
        pass

    async def run(self) -> None:
        while not self.state.should_shutdown():
            try:
                await self._poll_loop()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                import traceback
                logger.warning("Feed reconnecting: %s\n%s", exc, traceback.format_exc())
                self.state.set_feed_status(False, {"error": str(exc), "reconnecting": True})
                await asyncio.sleep(1.0)

    async def _poll_loop(self) -> None:
        while not self.state.should_shutdown():
            market = await asyncio.to_thread(get_current_candle, self.config.asset)
            if market is None:
                self._push_provisional_now()
                self.state.set_feed_status(False, {"error": "no active market"})
                await asyncio.sleep(ORDERBOOK_POLL_SEC)
                continue

            if market.slug != self._active_slug:
                await self._switch_candle(market)

            await self._poll_orderbook(market)
            await self._candle_watch_check(market)
            await asyncio.sleep(ORDERBOOK_POLL_SEC)

    async def _poll_orderbook(self, market: CandleMarket) -> None:
        if market.market_id <= 0:
            return
        try:
            book = await asyncio.to_thread(self._client.get_order_book, market.market_id)
            bids = _normalize_levels(book.get("bids", []))
            asks = _normalize_levels(book.get("asks", []))
            if bids or asks:
                self.state.update_feed_book("up", bids, asks)
                no_bids = _complement_levels(asks, market.tick_size)
                no_asks = _complement_levels(bids, market.tick_size)
                self.state.update_feed_book("down", no_bids, no_asks)
                self.state.set_feed_status(True)
        except Exception as exc:
            logger.debug("Orderbook poll failed: %s", exc)

    async def _seed_prices_from_category(self, market: CandleMarket) -> None:
        cat_raw = await asyncio.to_thread(self._client.get_category, market.slug) if self._client else None
        if not cat_raw:
            return
        markets = cat_raw.get("markets", [])
        if not markets:
            return
        m = markets[0]
        outcomes = m.get("outcomes", [])
        up = next((o for o in outcomes if o.get("name") == "Up"), None)
        down = next((o for o in outcomes if o.get("name") == "Down"), None)

        def _extract_price(obj, field: str) -> float | None:
            val = obj.get(field) if obj else None
            if val is None:
                return None
            if isinstance(val, dict):
                val = val.get("price") or val.get("value") or next(iter(val.values()), None)
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        up_px = _extract_price(up, "bestBid") or _extract_price(up, "bestAsk")
        down_px = _extract_price(down, "bestBid") or _extract_price(down, "bestAsk")

        if up_px is None or down_px is None:
            op = m.get("outcomePrices")
            if op and isinstance(op, (list, str)):
                import json
                if isinstance(op, str):
                    op = json.loads(op)
                if isinstance(op, (list, tuple)) and len(op) == 2:
                    try:
                        up_px = float(op[0])
                        down_px = float(op[1])
                    except (TypeError, ValueError):
                        pass
        if up_px is not None and down_px is not None:
            # Seed a single reference price per side. Passing a synthetic
            # complement as the ask would force mid=(px+(1-px))/2=0.5.
            self.state.update_feed_best("up", up_px, up_px)
            self.state.update_feed_best("down", down_px, down_px)

    def _push_provisional_now(self) -> None:
        start = expected_candle_start_ts()
        if self._provisional_pushed_for == start:
            return
        self._provisional_pushed_for = start
        stub = provisional_market_dict(self.config.asset, start)
        self.state.update(
            market=stub,
            signal={"side": "hold", "reason": "Rolling into new candle…"},
        )
        self.state.log("info", f"Provisional candle window {start} (awaiting market listing)")

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
        nxt = await asyncio.to_thread(
            get_next_candle, self.config.asset, self._active_market, max_wait_sec=0.35,
        )
        if nxt is not None:
            self._next_market = nxt
            logger.info("Prefetched next candle %s", nxt.slug)

    async def _candle_watch_check(self, market: CandleMarket) -> None:
        await self._prefetch_next()

        secs = market.seconds_to_close
        clock_start = expected_candle_start_ts()
        window_rolled = (
            market.candle_start_ts is not None
            and clock_start > market.candle_start_ts
        )

        if secs > 0.2 and not window_rolled:
            return

        self._push_provisional_now()

        if self._switching:
            return

        nxt = self._next_market
        if nxt is None or nxt.candle_start_ts < clock_start:
            nxt = await asyncio.to_thread(
                get_next_candle, self.config.asset, market, max_wait_sec=0.5,
            )

        if nxt is None or nxt.seconds_to_close <= 0:
            nxt = await asyncio.to_thread(get_current_candle, self.config.asset)

        if nxt is None or nxt.slug == self._active_slug:
            await asyncio.sleep(0.2)
            return

        self._switching = True
        try:
            self._next_market = None
            await self._switch_candle(nxt)
        finally:
            self._switching = False

    async def _switch_candle(self, market: CandleMarket) -> None:
        prev_start = (
            self._active_market.candle_start_ts if self._active_market is not None else None
        )
        self._active_slug = market.slug
        self._active_market = market
        self._provisional_pushed_for = market.candle_start_ts

        self.state.clear_market_data(closing_start_ts=prev_start)
        await self._init_candle_beat_and_history(market)
        self.state.update(
            market=market_to_dict(market),
            prices=None,
            signal={"side": "hold", "reason": "New candle — warming up"},
        )
        self.state.log("info", f"New candle: {market.title}")
        await self._bootstrap_book(market)
        await self._seed_prices_from_category(market)

    async def _bootstrap_book(self, market: CandleMarket) -> None:
        if market.market_id <= 0:
            return
        try:
            book = await asyncio.to_thread(self._client.get_order_book, market.market_id)
            bids = _normalize_levels(book.get("bids", []))
            asks = _normalize_levels(book.get("asks", []))
            if bids:
                self.state.update_feed_book("up", bids, asks)
            if asks:
                no_bids = _complement_levels(asks, market.tick_size)
                no_asks = _complement_levels(bids, market.tick_size)
                self.state.update_feed_book("down", no_bids, no_asks)
        except Exception as exc:
            logger.warning("REST book bootstrap unavailable: %s", exc)

    async def _init_candle_beat_and_history(self, market: CandleMarket) -> None:
        import time as _time
        cs = market.candle_start_ts
        now = _time.time()
        elapsed = now - cs

        from .btc_feed import fetch_candle_history, fetch_candle_open_price, fetch_outcome_price_history

        open_px = await asyncio.to_thread(fetch_candle_open_price, self.config.asset, cs)

        if open_px is not None:
            if elapsed > 1.0:
                btc_hist, outcome_hist = await asyncio.gather(
                    asyncio.to_thread(fetch_candle_history, self.config.asset, cs, now),
                    asyncio.to_thread(
                        fetch_outcome_price_history,
                        market.up_token_id,
                        market.down_token_id,
                        cs,
                        now,
                    ),
                )
                self.state.backfill_btc_history(cs, open_px, btc_hist)
                self.state.backfill_price_history(outcome_hist)
                logger.info(
                    "Backfilled candle %s open_px=%.2f with %d BTC points and %d outcome points",
                    cs, open_px, len(btc_hist), len(outcome_hist),
                )
            else:
                self.state.set_price_to_beat(open_px, candle_start_ts=cs, force=True)
                logger.info("Locked candle %s open_px=%.2f from REST", cs, open_px)
            return

        beat = self.state.get_btc_price()
        if beat is not None:
            self.state.set_price_to_beat(beat, candle_start_ts=cs, estimated=(elapsed > 30))


def _normalize_levels(levels: list) -> list[dict]:
    result = []
    if not isinstance(levels, list):
        return result
    for lvl in levels:
        if not lvl:
            continue
        if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            try:
                price = float(lvl[0])
                size = float(lvl[1])
            except (TypeError, ValueError):
                continue
        elif isinstance(lvl, dict):
            raw_price = lvl.get("price", 0)
            raw_size = lvl.get("size", 0)
            if isinstance(raw_price, dict) or isinstance(raw_size, dict):
                continue
            try:
                price = float(raw_price)
                size = float(raw_size)
            except (TypeError, ValueError):
                continue
        else:
            continue
        if size > 0:
            result.append({"price": price, "size": size})
    return result


def _complement_levels(levels: list[dict], decimal_precision: str = "0.01") -> list[dict]:
    if not levels:
        return []
    prec = int(1 / max(0.001, float(decimal_precision))) if decimal_precision else 100
    result = []
    for lvl in levels:
        if not isinstance(lvl, dict):
            continue
        raw_price = lvl.get("price", 0)
        if isinstance(raw_price, dict):
            continue
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        comp = (prec - round(price * prec)) / prec
        if comp >= 0:
            result.append({"price": comp, "size": float(lvl.get("size", 0))})
    return result