"""1-second BTC series for backtests, aligned with the live bot's signal.

The live bot evaluates on Binance spot (``BtcPriceFeed``) and locks the candle's
"price to beat" to the Binance 5-minute candle open (``fetch_candle_open_price``).
This module reconstructs both from cached Binance 1-second klines so a backtest
sees the same speed signal the bot does, instead of 1-minute closes.
"""

from __future__ import annotations

import bisect
import logging
from typing import Callable

logger = logging.getLogger(__name__)


class BtcSeries:
    """Sorted 1-second OHLC series with O(log n) lookups."""

    def __init__(self, secs: list[int], opens: list[float], closes: list[float], source: str):
        self._secs = secs
        self._opens = opens
        self._closes = closes
        self.source = source

    @classmethod
    def load(
        cls,
        start_ts: int,
        end_ts: int,
        *,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> "BtcSeries | None":
        from src.ml.btc_1s import load_1s_range

        try:
            frame = load_1s_range(
                int(start_ts) - 60,
                int(end_ts) + 60,
                history_pad_seconds=60,
                progress_callback=progress_callback,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to no BTC overlay
            logger.warning("BTC 1s data unavailable for batch: %s", exc)
            return None

        if frame is None or len(frame) == 0:
            return None

        secs = [int(ts.timestamp()) for ts in frame["timestamp"]]
        opens = [float(x) for x in frame["open"]]
        closes = [float(x) for x in frame["close"]]
        return cls(secs, opens, closes, source="binance_1s")

    def _index_at_or_before(self, t: int) -> int | None:
        idx = bisect.bisect_right(self._secs, int(t)) - 1
        if 0 <= idx < len(self._secs):
            return idx
        return None

    def price_at(self, t: int) -> float | None:
        """Last traded spot at or before ``t`` (1s close)."""
        idx = self._index_at_or_before(t)
        if idx is None:
            return None
        return self._closes[idx]

    def open_at(self, candle_start_ts: int) -> float | None:
        """Open of the first 1s bar at/after the candle start — the price to beat."""
        idx = bisect.bisect_left(self._secs, int(candle_start_ts))
        if idx >= len(self._secs):
            idx = self._index_at_or_before(candle_start_ts)
        if idx is None or idx >= len(self._secs):
            return None
        return self._opens[idx]
