"""Spot 1m klines from Binance for sizing features (asset-aware)."""

from __future__ import annotations

import bisect
import json
import logging
import time
from pathlib import Path

import numpy as np
import requests

logger = logging.getLogger(__name__)

BINANCE_URL = "https://api.binance.com/api/v3/klines"
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "btc_klines"
HEADERS = {"User-Agent": "signull-ml/0.1"}

# (open_time_ms, open, high, low, close, volume)
Kline = tuple[int, float, float, float, float, float]

BINANCE_SYMBOLS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}


def _cache_path(start_ms: int, end_ms: int, symbol: str = "BTCUSDT") -> Path:
    if symbol == "BTCUSDT":
        # Preserve legacy BTC cache filenames
        return CACHE_DIR / f"{start_ms}_{end_ms}.json"
    return CACHE_DIR / f"{symbol.lower()}_{start_ms}_{end_ms}.json"


def _is_sorted_by_open(klines: list[Kline]) -> bool:
    if len(klines) <= 1:
        return True
    prev = klines[0][0]
    for k in klines[1:]:
        ot = k[0]
        if ot < prev:
            return False
        prev = ot
    return True


def fetch_klines(
    start_ts: int,
    end_ts: int,
    *,
    use_cache: bool = True,
    asset: str = "btc",
) -> list[Kline]:
    """Fetch 1m klines covering [start_ts, end_ts] (unix seconds) for *asset*."""
    asset_key = (asset or "btc").lower()
    symbol = BINANCE_SYMBOLS.get(asset_key)
    if symbol is None:
        raise ValueError(
            f"Unsupported asset for klines: {asset!r}; "
            f"supported: {sorted(BINANCE_SYMBOLS)}"
        )

    start_ms = start_ts * 1000
    end_ms = end_ts * 1000
    cache = _cache_path(start_ms, end_ms, symbol)
    if use_cache and cache.exists():
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
            if not raw:
                # Empty file is not a successful full-range cache
                pass
            else:
                return [tuple(x) for x in raw]
        except Exception:
            pass

    rows: list[Kline] = []
    cursor = start_ms
    fetch_ok = True
    while cursor < end_ms:
        try:
            resp = requests.get(
                BINANCE_URL,
                params={
                    "symbol": symbol,
                    "interval": "1m",
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
                headers=HEADERS,
                timeout=20,
            )
            resp.raise_for_status()
            batch = resp.json()
        except requests.RequestException as exc:
            logger.warning("Binance klines failed (%s): %s", symbol, exc)
            fetch_ok = False
            break

        if not batch:
            break

        for k in batch:
            ot = int(k[0])
            rows.append((
                ot,
                float(k[1]),
                float(k[2]),
                float(k[3]),
                float(k[4]),
                float(k[5]),
            ))

        last_open = int(batch[-1][0])
        next_cursor = last_open + 60_000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.05)

    # Only persist a successful, non-empty fetch so failures never poison cache.
    if fetch_ok and rows:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(rows), encoding="utf-8")
    return rows


def window_features(
    klines: list[Kline],
    entry_ts: int,
    *,
    lookback: int = 60,
) -> np.ndarray | None:
    """
    Build (lookback, 3) feature matrix ending at entry_ts.

    Channels: log_return, range_pct, body_pct

    If *klines* is already sorted by open time (the common bulk-preload path),
    uses binary search instead of a full re-sort.
    """
    if not klines:
        return None

    entry_ms = entry_ts * 1000
    # Closed bars only: OHLCV is final after the one-minute interval ends.
    max_open_ms = entry_ms - 60_000

    if _is_sorted_by_open(klines):
        # Rightmost open_time <= max_open_ms via bisect on open times
        opens = [k[0] for k in klines]
        end_idx = bisect.bisect_right(opens, max_open_ms)
        if end_idx < lookback + 1:
            return None
        start_idx = end_idx - (lookback + 1)
        bars = klines[start_idx:end_idx]
    else:
        bars = sorted(
            [k for k in klines if k[0] <= max_open_ms],
            key=lambda x: x[0],
        )
        if len(bars) < lookback + 1:
            return None
        bars = bars[-(lookback + 1):]

    if len(bars) < lookback + 1:
        return None

    feats = np.zeros((lookback, 3), dtype=np.float32)

    for i in range(1, len(bars)):
        _, o, h, l, c, _ = bars[i]
        prev_c = bars[i - 1][4]
        lr = np.log(c / prev_c) if prev_c > 0 else 0.0
        rng = (h - l) / c if c > 0 else 0.0
        body = (c - o) / c if c > 0 else 0.0
        feats[i - 1] = [lr, rng, body]

    return feats


def btc_momentum_align(feats: np.ndarray, side: str) -> float:
    """0-1 score: spot trend alignment with bet side."""
    if feats is None or len(feats) == 0:
        return 0.5
    ret = float(np.sum(feats[:, 0]))
    if side == "up":
        score = ret
    else:
        score = -ret
    return float(1.0 / (1.0 + np.exp(-score * 80.0)))
