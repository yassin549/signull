"""BTC 1m klines from Binance for TCN sizing features."""

from __future__ import annotations

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


def _cache_path(start_ms: int, end_ms: int) -> Path:
    return CACHE_DIR / f"{start_ms}_{end_ms}.json"


def fetch_klines(start_ts: int, end_ts: int, *, use_cache: bool = True) -> list[Kline]:
    """Fetch 1m BTCUSDT klines covering [start_ts, end_ts] (unix seconds)."""
    start_ms = start_ts * 1000
    end_ms = end_ts * 1000
    cache = _cache_path(start_ms, end_ms)
    if use_cache and cache.exists():
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
            return [tuple(x) for x in raw]
        except Exception:
            pass

    rows: list[Kline] = []
    cursor = start_ms
    while cursor < end_ms:
        try:
            resp = requests.get(
                BINANCE_URL,
                params={
                    "symbol": "BTCUSDT",
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
            logger.warning("Binance klines failed: %s", exc)
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
    """
    entry_ms = entry_ts * 1000
    # OHLCV is only final after the one-minute interval closes.  Including an
    # open bar leaks future high/low/close values into historical backtests.
    bars = sorted([k for k in klines if k[0] + 60_000 <= entry_ms], key=lambda x: x[0])
    if len(bars) < lookback + 1:
        return None

    bars = bars[-(lookback + 1):]
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
    """0-1 score: BTC trend alignment with bet side."""
    if feats is None or len(feats) == 0:
        return 0.5
    ret = float(np.sum(feats[:, 0]))
    if side == "up":
        score = ret
    else:
        score = -ret
    return float(1.0 / (1.0 + np.exp(-score * 80.0)))
