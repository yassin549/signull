"""Discover active 5-minute Up/Down markets via the Predict.fun REST API."""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import EVENT_SLUG_PREFIX, SERIES_SLUGS, PREDICT_API_HOST

logger = logging.getLogger(__name__)

_PREDICT_API_KEY = os.getenv("PREDICT_API_KEY", "")
_IS_MAINNET = "testnet" not in PREDICT_API_HOST.lower()

HEADERS = {"User-Agent": "signull-bot/0.1"}
if _IS_MAINNET and _PREDICT_API_KEY:
    HEADERS["x-api-key"] = _PREDICT_API_KEY
CANDLE_DURATION = 300
MIN_ORDERS_BOOTSTRAP = 2


@dataclass
class CandleMarket:
    slug: str
    title: str
    end_date: datetime
    condition_id: str
    up_token_id: str
    down_token_id: str
    up_price: float
    down_price: float
    tick_size: str
    accepting_orders: bool
    market_id: int = 0
    fee_rate_bps: int = 0

    @property
    def seconds_to_close(self) -> float:
        return (self.end_date - datetime.now(timezone.utc)).total_seconds()

    @property
    def candle_start_ts(self) -> int:
        return int(self.end_date.timestamp()) - CANDLE_DURATION

    @property
    def candle_duration_sec(self) -> int:
        return CANDLE_DURATION


def _slug_for(asset: str, start_ts: int) -> str:
    return f"{EVENT_SLUG_PREFIX[asset]}-updown-5m-{start_ts}"


def _safe_load_category(slug: str) -> dict | None:
    try:
        import httpx
        resp = httpx.get(
            f"{PREDICT_API_HOST}/v1/categories/{slug}",
            headers=HEADERS,
            timeout=8,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("data")
    except Exception as exc:
        logger.debug("Predict.fun category load failed for %s: %s", slug, exc)
        return None


def _category_to_market(category: dict) -> CandleMarket | None:
    markets = category.get("markets", [])
    if not markets:
        return None
    m = markets[0]
    outcomes = m.get("outcomes", [])
    if not outcomes:
        return None
    up_out = next((o for o in outcomes if o.get("name") == "Up"), None)
    down_out = next((o for o in outcomes if o.get("name") == "Down"), None)
    if not up_out or not down_out:
        return None
    end = datetime.fromisoformat(category["endsAt"].replace("Z", "+00:00"))
    tick = "0.01"
    slug = category.get("slug", "")
    return CandleMarket(
        slug=slug,
        title=category.get("title", slug),
        end_date=end,
        condition_id=m.get("conditionId", ""),
        up_token_id=up_out.get("onChainId", ""),
        down_token_id=down_out.get("onChainId", ""),
        up_price=0.5,
        down_price=0.5,
        tick_size=tick,
        accepting_orders=m.get("tradingStatus", "") == "OPEN",
        market_id=m.get("id", 0),
        fee_rate_bps=m.get("feeRateBps", 0),
    )


def get_candle_at(asset: str, start_ts: int) -> CandleMarket | None:
    slug = _slug_for(asset, start_ts)
    cat = _safe_load_category(slug)
    if cat is None:
        return None
    m = _category_to_market(cat)
    if m is not None and m.seconds_to_close > 0:
        return m
    return None


def expected_candle_start_ts(now: float | None = None) -> int:
    t = time.time() if now is None else float(now)
    return math.floor(t / CANDLE_DURATION) * CANDLE_DURATION


def provisional_market_dict(asset: str, start_ts: int | None = None) -> dict:
    start = int(start_ts if start_ts is not None else expected_candle_start_ts())
    end_ts = start + CANDLE_DURATION
    end = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    secs = max(0.0, (end - now).total_seconds())
    return {
        "slug": _slug_for(asset, start),
        "title": f"{asset.upper()} Up or Down 5m (loading…)",
        "end_date": end.isoformat(),
        "condition_id": "",
        "up_token_id": "",
        "down_token_id": "",
        "up_price": 0.5,
        "down_price": 0.5,
        "tick_size": "0.01",
        "accepting_orders": False,
        "seconds_to_close": secs,
        "candle_start_ts": start,
        "candle_duration_sec": CANDLE_DURATION,
        "provisional": True,
    }


def get_next_candle(
    asset: str,
    current: CandleMarket | None = None,
    *,
    max_wait_sec: float = 0.6,
) -> CandleMarket | None:
    if current is not None:
        targets = [current.candle_start_ts + CANDLE_DURATION]
    else:
        base = expected_candle_start_ts()
        targets = [base, base + CANDLE_DURATION]

    for start_ts in targets:
        market = get_candle_at(asset, start_ts)
        if market is not None and market.seconds_to_close > 0:
            return market

    start_ts = targets[-1]
    deadline = time.time() + max(0.0, max_wait_sec)
    while time.time() < deadline:
        market = get_candle_at(asset, start_ts)
        if market is not None:
            return market
        time.sleep(0.05)

    return None


def get_current_candle(asset: str) -> CandleMarket | None:
    start_ts = expected_candle_start_ts()

    for candidate_ts in (start_ts, start_ts - CANDLE_DURATION):
        market = get_candle_at(asset, candidate_ts)
        if market is not None and market.seconds_to_close > 0:
            return market

    next_market = get_next_candle(asset, max_wait_sec=0.4)
    if next_market is not None:
        return next_market

    return None


def market_to_dict(market: CandleMarket) -> dict:
    return {
        "slug": market.slug,
        "title": market.title,
        "end_date": market.end_date.isoformat(),
        "condition_id": market.condition_id,
        "up_token_id": market.up_token_id,
        "down_token_id": market.down_token_id,
        "up_price": market.up_price,
        "down_price": market.down_price,
        "tick_size": market.tick_size,
        "accepting_orders": market.accepting_orders,
        "seconds_to_close": market.seconds_to_close,
        "candle_start_ts": market.candle_start_ts,
        "candle_duration_sec": market.candle_duration_sec,
        "market_id": market.market_id,
        "fee_rate_bps": market.fee_rate_bps,
    }


def resolve_candle_winner(
    asset: str,
    start_ts: int,
    *,
    require_resolved: bool = True,
) -> str | None:
    slug = _slug_for(asset, int(start_ts))
    cat = _safe_load_category(slug)
    if cat is None:
        return None
    markets = cat.get("markets", [])
    if not markets:
        return None
    m = markets[0]
    resolution = m.get("resolution")
    if resolution is not None:
        r_str = str(resolution).lower()
        if r_str in ("up", "down"):
            return r_str
    if not require_resolved:
        outcomes = m.get("outcomes", [])
        up_px = None
        down_px = None
        for o in outcomes:
            if o.get("name") == "Up":
                up_px = o.get("bestBid") or o.get("bestAsk")
            elif o.get("name") == "Down":
                down_px = o.get("bestBid") or o.get("bestAsk")
        if up_px is not None and down_px is not None:
            try:
                up_f, down_f = float(up_px), float(down_px)
                return _winner_from_prices(up_f, down_f, strict=require_resolved)
            except (TypeError, ValueError):
                pass
    return None


def _winner_from_prices(
    up: float, down: float, *, strict: bool = True
) -> str | None:
    if up >= 0.95 and down <= 0.05:
        return "up"
    if down >= 0.95 and up <= 0.05:
        return "down"
    if not strict:
        if up >= 0.90 and up > down:
            return "up"
        if down >= 0.90 and down > up:
            return "down"
    return None


def winner_from_price_refs(
    refs: dict[str, float | None],
) -> tuple[str | None, str]:
    beat = refs.get("beat")
    ref = refs.get("chainlink")
    source = "chainlink"
    if ref is None:
        ref = refs.get("spot")
        source = "spot"
    if beat is None or ref is None:
        return None, "none"
    side = "up" if float(ref) >= float(beat) else "down"
    return side, f"btc(beat={float(beat):.2f},{source}={float(ref):.2f})"


def winner_from_ticks(
    ticks: list[tuple[int, float, float]],
    *,
    at_close: bool = False,
) -> str | None:
    if not ticks:
        return None
    lead_threshold = 0.80 if at_close else 0.90
    extreme_threshold = 0.92 if at_close else 0.95
    tail = ticks[-5:]
    up = sum(t[1] for t in tail) / len(tail)
    down = sum(t[2] for t in tail) / len(tail)
    if up >= lead_threshold and up > down:
        return "up"
    if down >= lead_threshold and down > up:
        return "down"
    _t, lu, ld = ticks[-1]
    if lu >= extreme_threshold and lu > ld:
        return "up"
    if ld >= extreme_threshold and ld > lu:
        return "down"
    return None