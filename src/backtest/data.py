"""Fetch and cache historical 5M candle data from the Predict.fun API.

Replaces the old Polymarket loader.  For each 5-minute market we pull:

* ``GET /v1/categories/{slug}`` — market id, authoritative winner, fee rate.
* ``GET /v1/orders/matches``   — the real order-match tape (price in wei,
  side, size, ms timestamp), paginated via cursor.

The tape becomes the candle's ``ticks`` (``(t, up, down)`` real trade prints),
so the backtest engine evaluates strategies on the exact prices that traded on
the venue the live bot uses.  If no tape is available for a market we fall back
to the low-resolution ``chance`` timeseries and mark the candle ``synthetic``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

from src.config import EVENT_SLUG_PREFIX, PREDICT_API_HOST

from .types import CandleDataset

logger = logging.getLogger(__name__)

CANDLE_DURATION = 300
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "backtest_cache_predict"
MATCH_PAGE = 200
MAX_MATCH_PAGES = 200
REQUEST_ATTEMPTS = 4
WEI = 1e18

_HEADERS = {"User-Agent": "signull-backtest/0.2", "Content-Type": "application/json"}


def _api_key() -> str:
    return os.getenv("PREDICT_API_KEY", "") or ""


def _headers() -> dict[str, str]:
    headers = dict(_HEADERS)
    key = _api_key()
    if key:
        headers["x-api-key"] = key
    return headers


def _get(path: str, params: dict | None = None) -> dict | None:
    url = f"{PREDICT_API_HOST}{path}"
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            with httpx.Client(timeout=25.0) as client:
                resp = client.get(url, params=params, headers=_headers())
            if resp.status_code == 404:
                return None
            if resp.status_code in (408, 429) or resp.status_code >= 500:
                logger.debug("Transient HTTP %s from %s", resp.status_code, path)
            else:
                resp.raise_for_status()
                return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("Request failed %s: %s", path, exc)
        if attempt + 1 < REQUEST_ATTEMPTS:
            time.sleep(0.4 * (2**attempt))
    return None


def _slug_for(asset: str, start_ts: int) -> str:
    return f"{EVENT_SLUG_PREFIX[asset]}-updown-5m-{start_ts}"


def _latest_resolved_start(*, skip_open: int = 2) -> int:
    now = datetime.now(timezone.utc).timestamp()
    return int(math.floor(now / CANDLE_DURATION) * CANDLE_DURATION - skip_open * CANDLE_DURATION)


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _to_float_wei(value) -> float:
    if value is None:
        return 0.0
    try:
        return float(value) / WEI
    except (TypeError, ValueError):
        return 0.0


def _winner_from_category(category: dict) -> str | None:
    markets = category.get("markets") or []
    if not markets:
        return None
    market = markets[0]
    for outcome in market.get("outcomes") or []:
        if str(outcome.get("status", "")).upper() == "WON":
            name = str(outcome.get("name", "")).lower()
            if name in ("up", "down"):
                return name
    resolution = market.get("resolution")
    if isinstance(resolution, str):
        low = resolution.lower()
        if low in ("up", "down"):
            return low
    return None


def _outcome_ids(market: dict) -> tuple[str, str]:
    up_id = down_id = ""
    for outcome in market.get("outcomes") or []:
        name = str(outcome.get("name", "")).lower()
        if name == "up":
            up_id = str(outcome.get("onChainId", ""))
        elif name == "down":
            down_id = str(outcome.get("onChainId", ""))
    return up_id, down_id


def _fetch_category(asset: str, start_ts: int) -> dict | None:
    payload = _get(f"/v1/categories/{_slug_for(asset, start_ts)}")
    if payload is None:
        return None
    if isinstance(payload, dict) and "data" in payload:
        data = payload.get("data")
        return data if isinstance(data, dict) else None
    return payload


def _fetch_match_tape(market_id: int, start_ts: int, end_ts: int) -> list[tuple[int, float, float, str]]:
    """Return real trade prints as ``(t_seconds, up, down, up_quote)``."""
    prints: list[tuple[float, float, str]] = []
    cursor: str | None = None
    for _ in range(MAX_MATCH_PAGES):
        params: dict = {
            "marketId": market_id,
            "first": MATCH_PAGE,
            "executedAfter": int(start_ts) * 1000,
            "executedBefore": int(end_ts) * 1000,
        }
        if cursor:
            params["after"] = cursor
        payload = _get("/v1/orders/matches", params)
        if payload is None:
            break
        rows = payload.get("data") or []
        for row in rows:
            ts = _parse_ts(row.get("executedAt"))
            if ts is None:
                continue
            taker = row.get("taker") or {}
            taker_out = str((taker.get("outcome") or {}).get("name", "")).lower()
            taker_price = _to_float_wei(taker.get("price"))
            up_price: float | None = None
            up_quote = "ask" if taker_out == "up" else "bid"
            if taker_out == "up":
                up_price = taker_price
            else:
                for maker in row.get("makers") or []:
                    maker_out = str((maker.get("outcome") or {}).get("name", "")).lower()
                    if maker_out == "up":
                        up_price = _to_float_wei(maker.get("price"))
                        break
                if up_price is None and 0.0 < taker_price < 1.0:
                    # Taker bought Down, so Up == 1 - down_price.
                    up_price = 1.0 - taker_price
            if up_price is None or not (0.0 < up_price < 1.0):
                continue
            prints.append((ts, up_price, up_quote))

        cursor = payload.get("cursor")
        if not cursor or not rows:
            break

    prints.sort(key=lambda p: p[0])
    # Deduplicate identical consecutive prints to keep the tick list compact.
    ticks: list[tuple[int, float, float, str]] = []
    for ts, up, quote in prints:
        down = 1.0 - up
        t = int(ts)
        if ticks and ticks[-1][0] == t and abs(ticks[-1][1] - up) < 1e-9:
            continue
        ticks.append((t, round(up, 4), round(down, 4), quote))
    return ticks


def _fetch_chance_series(market_id: int, start_ts: int, end_ts: int) -> list[tuple[int, float, float]]:
    """Fallback low-resolution chance series (Up probability 0-100)."""
    payload = _get(
        f"/v1/markets/{market_id}/timeseries",
        {"metric": "chance", "resolution": "1m", "from": start_ts - 120, "to": end_ts + 120},
    )
    if payload is None:
        return []
    series = (payload.get("data") or {}).get("series") or []
    ticks: list[tuple[int, float, float]] = []
    for point in series:
        try:
            t = int(float(point["x"]))
            up = float(point["y"]) / 100.0
        except (KeyError, TypeError, ValueError):
            continue
        if not (0.0 < up < 1.0):
            continue
        if t < start_ts or t > end_ts:
            continue
        ticks.append((t, round(up, 4), round(1.0 - up, 4)))
    return ticks


def _cache_path(asset: str, start_ts: int) -> Path:
    return CACHE_DIR / asset / f"{start_ts}.json"


def _load_cache(asset: str, start_ts: int) -> CandleDataset | None:
    path = _cache_path(asset, start_ts)
    if not path.exists():
        return None
    try:
        return CandleDataset.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _save_cache(dataset: CandleDataset, asset: str) -> None:
    path = _cache_path(asset, dataset.start_ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dataset.to_dict(), indent=1), encoding="utf-8")


def _build_candle(asset: str, start_ts: int, *, use_cache: bool) -> CandleDataset | None:
    if use_cache:
        cached = _load_cache(asset, start_ts)
        if cached is not None:
            return cached

    category = _fetch_category(asset, start_ts)
    if category is None:
        return None
    markets = category.get("markets") or []
    if not markets:
        return None
    market = markets[0]
    winner = _winner_from_category(category)
    if winner not in ("up", "down"):
        return None

    market_id = int(market.get("id", 0) or 0)
    fee_rate_bps = int(market.get("feeRateBps", 200) or 200)
    up_token_id, down_token_id = _outcome_ids(market)

    end_ts = start_ts + CANDLE_DURATION
    ends_at = _parse_ts(category.get("endsAt"))
    if ends_at is not None and ends_at > start_ts:
        end_ts = int(ends_at)

    synthetic = False
    tape = _fetch_match_tape(market_id, start_ts, end_ts) if market_id > 0 else []
    if len(tape) >= 2:
        ticks = [(t, up, down) for t, up, down, _quote in tape]
    else:
        ticks = _fetch_chance_series(market_id, start_ts, end_ts) if market_id > 0 else []
        synthetic = True
        if len(ticks) < 2:
            return None

    dataset = CandleDataset(
        slug=_slug_for(asset, start_ts),
        title=category.get("title") or _slug_for(asset, start_ts),
        start_ts=start_ts,
        end_ts=end_ts,
        winner=winner,
        up_token_id=up_token_id,
        down_token_id=down_token_id,
        ticks=ticks,
        market_id=market_id,
        fee_rate_bps=fee_rate_bps,
        synthetic=synthetic,
    )
    if use_cache:
        _save_cache(dataset, asset)
    return dataset


def list_candidate_starts(asset: str, count: int, *, skip_open: int = 2) -> list[int]:
    """Return recent candle start timestamps (oldest first)."""
    base = _latest_resolved_start(skip_open=skip_open)
    return [int(base - i * CANDLE_DURATION) for i in range(count - 1, -1, -1)]


def list_candidate_starts_in_range(start_ts: int, end_ts: int, *, skip_open: int = 2) -> list[int]:
    """Return every fully resolved 5-minute market in [start_ts, end_ts)."""
    if end_ts <= start_ts:
        return []
    first = math.ceil(start_ts / CANDLE_DURATION) * CANDLE_DURATION
    last_requested = math.floor((end_ts - CANDLE_DURATION) / CANDLE_DURATION) * CANDLE_DURATION
    last = min(last_requested, _latest_resolved_start(skip_open=skip_open))
    if first > last:
        return []
    return list(range(int(first), int(last) + 1, CANDLE_DURATION))


def first_available_start(asset: str, *, skip_open: int = 2, max_days: int = 120) -> int | None:
    """Find the earliest retained Predict.fun 5-minute market (bounded search)."""
    latest = _latest_resolved_start(skip_open=skip_open)
    floor = latest - max_days * 86400
    if _fetch_category(asset, latest) is None:
        return None
    if _fetch_category(asset, floor) is not None:
        return floor
    low, high = floor, latest
    while low < high:
        mid = ((low + high) // (2 * CANDLE_DURATION)) * CANDLE_DURATION
        if mid <= low:
            mid = low + CANDLE_DURATION
        if _fetch_category(asset, mid) is None:
            low = mid
        else:
            high = mid
    return high if _fetch_category(asset, high) is not None else None


def fetch_candles(
    asset: str = "btc",
    count: int = 100,
    *,
    start_ts: int | None = None,
    end_ts: int | None = None,
    all_history: bool = False,
    use_cache: bool = True,
    max_workers: int = 8,
    progress_callback: Callable[[dict], None] | None = None,
) -> list[CandleDataset]:
    """Load resolved Predict.fun candles, using disk cache when available."""
    if all_history:
        start_ts = first_available_start(asset)
        end_ts = _latest_resolved_start() + CANDLE_DURATION
        if start_ts is None:
            return []
    if (start_ts is None) != (end_ts is None):
        raise ValueError("start_ts and end_ts must be supplied together")
    starts = (
        list_candidate_starts_in_range(start_ts, end_ts)
        if start_ts is not None and end_ts is not None
        else list_candidate_starts(asset, count)
    )
    candles: list[CandleDataset] = []
    to_fetch: list[int] = []
    total = len(starts)
    completed = 0
    resolved = 0

    def report() -> None:
        if progress_callback is not None:
            progress_callback({
                "type": "progress", "phase": "loading",
                "candles_completed": completed, "candles_total": total,
                "candles_resolved": resolved,
            })

    for start in starts:
        if use_cache:
            cached = _load_cache(asset, start)
            if cached is not None:
                candles.append(cached)
                completed += 1
                resolved += 1
                if completed == total or completed % 10 == 0:
                    report()
                continue
        to_fetch.append(start)

    if to_fetch:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_build_candle, asset, ts, use_cache=use_cache): ts
                for ts in to_fetch
            }
            built: dict[int, CandleDataset] = {}
            for fut in as_completed(futures):
                ts = futures[fut]
                try:
                    ds = fut.result()
                    if ds is not None:
                        built[ts] = ds
                        resolved += 1
                except Exception:
                    logger.exception("Failed building candle %s", ts)
                completed += 1
                if completed == total or completed % 10 == 0:
                    report()

            for start in to_fetch:
                if start in built:
                    candles.append(built[start])

    candles.sort(key=lambda c: c.start_ts)
    report()
    return candles


def prefetch_progress(asset: str, count: int) -> dict:
    starts = list_candidate_starts(asset, count)
    cached = sum(1 for ts in starts if _cache_path(asset, ts).exists())
    return {"asset": asset, "requested": count, "cached": cached, "missing": count - cached}
