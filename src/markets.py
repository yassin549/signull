"""Discover active 5-minute Up/Down markets via the Gamma API."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import requests

from .config import EVENT_SLUG_PREFIX, GAMMA_HOST, SERIES_SLUGS

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "signull-bot/0.1"}
CANDLE_DURATION = 300


@dataclass
class CandleMarket:
    """A single 5-minute Up/Down candle market."""

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

    @property
    def seconds_to_close(self) -> float:
        return (self.end_date - datetime.now(timezone.utc)).total_seconds()

    @property
    def candle_start_ts(self) -> int:
        return int(self.end_date.timestamp()) - CANDLE_DURATION

    @property
    def candle_duration_sec(self) -> int:
        return CANDLE_DURATION


def _parse_json_field(value) -> list:
    if isinstance(value, str):
        return json.loads(value)
    return value or []


def _current_candle_start_ts() -> int:
    """Unix timestamp of the current 5M window's start (slug encodes this value)."""
    now = datetime.now(timezone.utc).timestamp()
    return math.floor(now / 300) * 300


def _slug_for(asset: str, start_ts: int) -> str:
    return f"{EVENT_SLUG_PREFIX[asset]}-updown-5m-{start_ts}"


def _safe_load_event(slug: str) -> CandleMarket | None:
    try:
        resp = requests.get(
            f"{GAMMA_HOST}/events/slug/{slug}",
            headers=HEADERS,
            timeout=8,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.debug("Gamma load failed for %s: %s", slug, exc)
        return None

    event = resp.json()
    markets = event.get("markets", [])
    if not markets:
        return None

    market = markets[0]
    outcomes = _parse_json_field(market.get("outcomes"))
    token_ids = _parse_json_field(market.get("clobTokenIds"))
    prices = _parse_json_field(market.get("outcomePrices"))

    if len(outcomes) != 2 or len(token_ids) != 2:
        return None

    up_idx = outcomes.index("Up") if "Up" in outcomes else 0
    down_idx = 1 - up_idx

    end = datetime.fromisoformat(event["endDate"].replace("Z", "+00:00"))
    tick = str(market.get("orderPriceMinTickSize", "0.01"))

    return CandleMarket(
        slug=slug,
        title=event["title"],
        end_date=end,
        condition_id=market["conditionId"],
        up_token_id=token_ids[up_idx],
        down_token_id=token_ids[down_idx],
        up_price=float(prices[up_idx]) if prices else 0.5,
        down_price=float(prices[down_idx]) if prices else 0.5,
        tick_size=tick,
        accepting_orders=bool(market.get("acceptingOrders", False)),
    )


def _load_event(slug: str) -> CandleMarket | None:
    return _safe_load_event(slug)


def get_candle_at(asset: str, start_ts: int) -> CandleMarket | None:
    return _safe_load_event(_slug_for(asset, start_ts))


def get_next_candle(asset: str, current: CandleMarket | None = None) -> CandleMarket | None:
    """Return the candle after *current*, or the best open window from now."""
    if current is not None:
        targets = [current.candle_start_ts + CANDLE_DURATION]
    else:
        base = _current_candle_start_ts()
        targets = [base, base + CANDLE_DURATION]

    for start_ts in targets:
        market = get_candle_at(asset, start_ts)
        if market is not None and market.seconds_to_close > 0:
            return market

    # Aggressive poll — next window is usually listed before the prior one closes.
    start_ts = targets[-1]
    for _ in range(40):
        market = get_candle_at(asset, start_ts)
        if market is not None:
            return market
        time.sleep(0.1)

    return _find_imminent_candle(asset)


def get_current_candle(asset: str) -> CandleMarket | None:
    """
    Return the active 5M candle whose window is currently open.

    Each candle's event slug is `{asset}-updown-5m-{start_unix_ts}` where the
    timestamp is the window start time aligned to 5-minute boundaries.
    """
    start_ts = _current_candle_start_ts()

    # Prefer the window that should be open right now.
    for candidate_ts in (start_ts, start_ts - CANDLE_DURATION):
        market = get_candle_at(asset, candidate_ts)
        if market is not None and market.seconds_to_close > 0:
            return market

    # Current window ended — jump to the next one immediately.
    next_market = get_next_candle(asset)
    if next_market is not None:
        return next_market

    return _find_imminent_candle(asset)


def _find_imminent_candle(asset: str) -> CandleMarket | None:
    """Series fallback: only return a candle ending within the next ~5 minutes."""
    series_slug = SERIES_SLUGS[asset]
    try:
        resp = requests.get(
            f"{GAMMA_HOST}/series",
            params={"slug": series_slug},
            headers=HEADERS,
            timeout=8,
        )
        resp.raise_for_status()
        series_list = resp.json()
    except requests.RequestException:
        return None

    if not series_list:
        return None

    now = datetime.now(timezone.utc)
    best_slug: str | None = None
    best_secs = float("inf")

    for event in series_list[0].get("events", []):
        if event.get("closed") or not event.get("active"):
            continue
        end = datetime.fromisoformat(event["endDate"].replace("Z", "+00:00"))
        secs = (end - now).total_seconds()
        # Only candles that are open now (not far-future listings).
        if 0 < secs <= CANDLE_DURATION + 20 and secs < best_secs:
            best_secs = secs
            best_slug = event["slug"]

    if best_slug:
        return _safe_load_event(best_slug)

    return None


def market_to_dict(market: CandleMarket) -> dict:
    data = asdict(market)
    data["end_date"] = market.end_date.isoformat()
    data["seconds_to_close"] = market.seconds_to_close
    data["candle_start_ts"] = market.candle_start_ts
    data["candle_duration_sec"] = market.candle_duration_sec
    return data