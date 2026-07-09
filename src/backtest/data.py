"""Fetch and cache historical 5M candle price data from Polymarket."""

from __future__ import annotations

import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.config import EVENT_SLUG_PREFIX, GAMMA_HOST

from .types import CandleDataset

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "signull-backtest/0.1"}
CLOB_HOST = "https://clob.polymarket.com"
CANDLE_DURATION = 300
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "backtest_cache"


def _parse_json_field(value) -> list:
    if isinstance(value, str):
        return json.loads(value)
    return value or []


def _slug_for(asset: str, start_ts: int) -> str:
    return f"{EVENT_SLUG_PREFIX[asset]}-updown-5m-{start_ts}"


def _winner_from_market(market: dict) -> str | None:
    outcomes = _parse_json_field(market.get("outcomes"))
    prices = _parse_json_field(market.get("outcomePrices"))
    if not outcomes or not prices:
        return None
    for i, raw in enumerate(prices):
        if float(raw) >= 0.99:
            return outcomes[i].lower()
    return None


def _fetch_event(slug: str) -> dict | None:
    try:
        resp = requests.get(f"{GAMMA_HOST}/events/slug/{slug}", headers=HEADERS, timeout=12)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        logger.debug("Gamma fetch failed %s: %s", slug, exc)
        return None


def _fetch_price_history(token_id: str, start_ts: int, end_ts: int) -> list[tuple[int, float]]:
    try:
        resp = requests.get(
            f"{CLOB_HOST}/prices-history",
            params={
                "market": token_id,
                "startTs": start_ts,
                "endTs": end_ts,
                "fidelity": 1,
            },
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        history = resp.json().get("history", [])
        points = [(int(p["t"]), float(p["p"])) for p in history if start_ts <= int(p["t"]) <= end_ts]
        if len(points) >= 3:
            return sorted(points, key=lambda x: x[0])

        # Fallback: pull max range and filter locally.
        resp = requests.get(
            f"{CLOB_HOST}/prices-history",
            params={"market": token_id, "interval": "max", "fidelity": 1},
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        history = resp.json().get("history", [])
        points = [(int(p["t"]), float(p["p"])) for p in history if start_ts <= int(p["t"]) <= end_ts]
        return sorted(points, key=lambda x: x[0])
    except requests.RequestException as exc:
        logger.debug("Price history failed %s: %s", token_id[:12], exc)
        return []


def _merge_ticks(
    up_pts: list[tuple[int, float]],
    down_pts: list[tuple[int, float]],
    start_ts: int,
    end_ts: int,
) -> list[tuple[int, float, float]]:
    """Forward-fill Up/Down prices on a unified timeline."""
    times = sorted({t for t, _ in up_pts} | {t for t, _ in down_pts})
    if not times:
        return []

    up_map = dict(up_pts)
    down_map = dict(down_pts)
    last_up = 0.5
    last_down = 0.5
    merged: list[tuple[int, float, float]] = []

    for t in times:
        if t < start_ts or t > end_ts:
            continue
        if t in up_map:
            last_up = up_map[t]
        if t in down_map:
            last_down = down_map[t]
        merged.append((t, last_up, last_down))

    return merged


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
    path.write_text(json.dumps(dataset.to_dict(), indent=2), encoding="utf-8")


def _build_candle(asset: str, start_ts: int, *, use_cache: bool) -> CandleDataset | None:
    if use_cache:
        cached = _load_cache(asset, start_ts)
        if cached is not None:
            return cached

    slug = _slug_for(asset, start_ts)
    event = _fetch_event(slug)
    if event is None:
        return None

    markets = event.get("markets", [])
    if not markets:
        return None

    market = markets[0]
    if not market.get("closed"):
        return None

    winner = _winner_from_market(market)
    if winner not in ("up", "down"):
        return None

    outcomes = _parse_json_field(market.get("outcomes"))
    token_ids = _parse_json_field(market.get("clobTokenIds"))
    if len(outcomes) != 2 or len(token_ids) != 2:
        return None

    up_idx = outcomes.index("Up") if "Up" in outcomes else 0
    down_idx = 1 - up_idx
    end_ts = int(datetime.fromisoformat(event["endDate"].replace("Z", "+00:00")).timestamp())
    end_ts = start_ts + CANDLE_DURATION if end_ts < start_ts else end_ts

    up_pts = _fetch_price_history(token_ids[up_idx], start_ts, end_ts)
    down_pts = _fetch_price_history(token_ids[down_idx], start_ts, end_ts)
    ticks = _merge_ticks(up_pts, down_pts, start_ts, end_ts)
    if len(ticks) < 2:
        return None

    dataset = CandleDataset(
        slug=slug,
        title=event.get("title", slug),
        start_ts=start_ts,
        end_ts=end_ts,
        winner=winner,
        up_token_id=token_ids[up_idx],
        down_token_id=token_ids[down_idx],
        ticks=ticks,
    )
    if use_cache:
        _save_cache(dataset, asset)
    return dataset


def list_candidate_starts(asset: str, count: int, *, skip_open: int = 2) -> list[int]:
    """Return recent candle start timestamps (oldest first)."""
    now = datetime.now(timezone.utc).timestamp()
    base = math.floor(now / CANDLE_DURATION) * CANDLE_DURATION
    return [int(base - (skip_open + i) * CANDLE_DURATION) for i in range(count - 1, -1, -1)]


def fetch_candles(
    asset: str = "btc",
    count: int = 100,
    *,
    use_cache: bool = True,
    max_workers: int = 8,
) -> list[CandleDataset]:
    """Load *count* resolved candles, using disk cache when available."""
    starts = list_candidate_starts(asset, count)
    candles: list[CandleDataset] = []
    to_fetch: list[int] = []

    for start_ts in starts:
        if use_cache and _load_cache(asset, start_ts) is not None:
            candles.append(_load_cache(asset, start_ts))  # type: ignore[arg-type]
        else:
            to_fetch.append(start_ts)

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
                except Exception:
                    logger.exception("Failed building candle %s", ts)

            for start_ts in to_fetch:
                if start_ts in built:
                    candles.append(built[start_ts])

    candles.sort(key=lambda c: c.start_ts)
    return candles


def prefetch_progress(asset: str, count: int) -> dict:
    starts = list_candidate_starts(asset, count)
    cached = sum(1 for ts in starts if _cache_path(asset, ts).exists())
    return {"asset": asset, "requested": count, "cached": cached, "missing": count - cached}