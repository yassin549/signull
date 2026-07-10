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
    return expected_candle_start_ts()


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


def expected_candle_start_ts(now: float | None = None) -> int:
    """UTC-aligned 5m window start for *now*."""
    t = time.time() if now is None else float(now)
    return math.floor(t / CANDLE_DURATION) * CANDLE_DURATION


def provisional_market_dict(asset: str, start_ts: int | None = None) -> dict:
    """
    Clock-based market stub so the dashboard can roll the countdown immediately
    even before Gamma lists the new event / WS resubscribes.
    """
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
    """Return the candle after *current*, or the best open window from now.

    *max_wait_sec* bounds Gamma polling so the feed/bot never block for tens
    of seconds waiting for a listing (dashboard uses clock fallback meanwhile).
    """
    if current is not None:
        targets = [current.candle_start_ts + CANDLE_DURATION]
    else:
        base = expected_candle_start_ts()
        targets = [base, base + CANDLE_DURATION]

    for start_ts in targets:
        market = get_candle_at(asset, start_ts)
        if market is not None and market.seconds_to_close > 0:
            return market

    # Short poll — next window is often listed just before/after the rollover.
    start_ts = targets[-1]
    deadline = time.time() + max(0.0, max_wait_sec)
    while time.time() < deadline:
        market = get_candle_at(asset, start_ts)
        if market is not None:
            return market
        time.sleep(0.05)

    return _find_imminent_candle(asset)


def get_current_candle(asset: str) -> CandleMarket | None:
    """
    Return the active 5M candle whose window is currently open.

    Each candle's event slug is `{asset}-updown-5m-{start_unix_ts}` where the
    timestamp is the window start time aligned to 5-minute boundaries.
    """
    start_ts = expected_candle_start_ts()

    # Prefer the window that should be open right now.
    for candidate_ts in (start_ts, start_ts - CANDLE_DURATION):
        market = get_candle_at(asset, candidate_ts)
        if market is not None and market.seconds_to_close > 0:
            return market

    # Current window ended — jump to the next one immediately (short wait).
    next_market = get_next_candle(asset, max_wait_sec=0.4)
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


def resolve_candle_winner(
    asset: str,
    start_ts: int,
    *,
    require_resolved: bool = True,
) -> str | None:
    """
    Resolve a closed 5m candle to "up" or "down" via Gamma outcome prices.

    When *require_resolved* is True (default), only accept a winner once a side
    is clearly settled (≥0.95). Mid-candle / pre-resolution prices like 0.67/0.33
    intentionally return None so callers can wait or use a better fallback.
    """
    slug = _slug_for(asset, int(start_ts))
    try:
        resp = requests.get(
            f"{GAMMA_HOST}/events/slug/{slug}",
            headers=HEADERS,
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        event = resp.json()
        markets = event.get("markets") or []
        if not markets:
            return None
        m = markets[0]
        outcomes = _parse_json_field(m.get("outcomes"))
        prices = _parse_json_field(m.get("outcomePrices"))
        if len(outcomes) != 2 or len(prices) != 2:
            return None
        up_idx = outcomes.index("Up") if "Up" in outcomes else 0
        down_idx = 1 - up_idx
        up_p = float(prices[up_idx])
        down_p = float(prices[down_idx])
        closed = bool(event.get("closed") or m.get("closed"))
        return _winner_from_prices(
            up_p,
            down_p,
            strict=require_resolved and not closed,
        )
    except (requests.RequestException, ValueError, TypeError, IndexError, KeyError):
        return None


def _winner_from_prices(
    up: float,
    down: float,
    *,
    strict: bool = True,
) -> str | None:
    """
    Map outcome prices to a winner.

    *strict*: only accept near-binary settlement (≥0.95). Soft mode (closed
    markets) still requires a clear lead (≥0.90) — never treat 0.67/0.33 as final.
    """
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


def winner_from_ticks(ticks: list[tuple[int, float, float]]) -> str | None:
    """Infer winner from the last path prints of a candle (near-close mids)."""
    if not ticks:
        return None
    # Prefer the last few samples in case the final print is noisy
    tail = ticks[-5:]
    up = sum(t[1] for t in tail) / len(tail)
    down = sum(t[2] for t in tail) / len(tail)
    if up >= 0.90 and up > down:
        return "up"
    if down >= 0.90 and down > up:
        return "down"
    # Extreme last print
    _t, lu, ld = ticks[-1]
    if lu >= 0.95 and lu > ld:
        return "up"
    if ld >= 0.95 and ld > lu:
        return "down"
    return None