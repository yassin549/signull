"""Signull 1.10 — regime-adaptive ±$10: follow in calm tape, invert in volatile tape.

Built on Signull 1.5.  When Bitcoin moves ±$10 from the candle open we still get
a directional "favorite", but whether we *take* that side depends on the market
regime measured from 1-minute klines strictly before the signal:

* calm tape  -> the ±$10 break tends to hold, so we follow the signal.
* volatile tape -> candles reverse often, so we take the opposite side.

Everything is causal: the volatility windows only use closed bars that ended
before the entry timestamp, so the backtest never peeks ahead.
"""

from __future__ import annotations

import bisect
import math

from strategies.base import CandleContext, StrategyMeta, TickContext, TradeSignal
from strategies.signull_1_5 import Signull15Strategy
from src.ml.btc_features import fetch_klines, Kline

STRATEGY_CLASS = "Signull110Strategy"


def _log_return_std(closes: list[float], start: int, end: int) -> float:
    """Std-dev of 1-bar log returns over ``closes[start:end]`` (0 if too few)."""
    lo = max(0, start)
    if end - lo < 2:
        return 0.0
    rets: list[float] = []
    for i in range(lo + 1, end):
        prev = closes[i - 1]
        cur = closes[i]
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    if not rets:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var)


class Signull110Strategy(Signull15Strategy):
    """Signull 1.5 signal, but inverted when the recent tape is volatile."""

    meta = StrategyMeta(
        id="signull_1_10",
        name="Signull 1.10 (Regime-Flip ±$10)",
        description=(
            "Signull 1.5's ±$10 trigger, but checks the recent BTC volatility "
            "regime from 1-minute klines: follows the break when the tape is "
            "calm and inverts it when the tape is volatile (reversals likely). "
            "Risks a fixed fraction of capital (default 10%)."
        ),
        default_params={
            "target_delta": 10.0,
            "risk_pct": 0.10,
            "vol_lookback_min": 30,
            "baseline_lookback_min": 180,
            "vol_multiplier": 1.2,
            "min_vol": 0.0,
            "invert_when_volatile": True,
            "asset": "btc",
        },
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._closes: list[float] = []

    def prepare_backtest(self, candles: list) -> None:
        self._candle_start_btc.clear()
        if not candles:
            self._klines = []
            self._kline_opens = []
            self._closes = []
            return
        base_min = int(self.params.get("baseline_lookback_min", 180))
        t_min = candles[0].start_ts - (base_min + 5) * 60
        t_max = candles[-1].end_ts + 300
        rows = fetch_klines(t_min, t_max, asset=self._asset) or []
        self._klines: list[Kline] = sorted(rows, key=lambda x: x[0])
        self._kline_opens = [k[0] for k in self._klines]
        self._closes = [float(k[4]) for k in self._klines]

    def _regime_at(self, t_sec: int) -> tuple[bool, float, float]:
        """Return (volatile, short_vol, baseline_vol) using only closed bars."""
        if len(self._closes) < 2 or not self._kline_opens:
            return False, 0.0, 0.0

        t_ms = int(t_sec) * 1000
        # Only bars that closed before the entry: open_time <= t - 60s.
        j = bisect.bisect_right(self._kline_opens, t_ms - 60_000)
        short_n = max(2, int(self.params.get("vol_lookback_min", 30)))
        base_n = max(short_n, int(self.params.get("baseline_lookback_min", 180)))

        short_vol = _log_return_std(self._closes, j - short_n, j)
        base_vol = _log_return_std(self._closes, j - base_n, j)

        mult = float(self.params.get("vol_multiplier", 1.2))
        min_vol = float(self.params.get("min_vol", 0.0))

        if base_vol > 0:
            volatile = (short_vol / base_vol >= mult) and short_vol >= min_vol
        else:
            volatile = short_vol > 0 and short_vol >= min_vol and min_vol > 0
        return volatile, short_vol, base_vol

    def evaluate(
        self, tick: TickContext, candle: CandleContext, *, entered: bool
    ) -> TradeSignal | None:
        if entered:
            return None

        current_btc = self._get_btc_price(tick, candle)
        if current_btc <= 0:
            return None

        if candle.slug not in self._candle_start_btc:
            beat = tick.btc_price_to_beat
            self._candle_start_btc[candle.slug] = (
                float(beat) if beat is not None and beat > 0 else current_btc
            )

        start_btc = self._candle_start_btc[candle.slug]
        delta = current_btc - start_btc
        target_delta = float(self.params.get("target_delta", 10.0))

        up_hit = delta >= target_delta
        down_hit = delta <= -target_delta
        if not up_hit and not down_hit:
            return None

        # The side the raw ±$10 break favors.
        if up_hit and down_hit:
            favorite = "up" if delta >= 0 else "down"
        else:
            favorite = "up" if up_hit else "down"

        volatile, short_vol, base_vol = self._regime_at(tick.t)
        invert_when_volatile = bool(self.params.get("invert_when_volatile", True))
        invert = volatile == invert_when_volatile

        side = favorite
        if invert:
            side = "down" if favorite == "up" else "up"

        price = tick.up if side == "up" else tick.down
        regime = "volatile" if volatile else "calm"
        action = "INVERT" if invert else "FOLLOW"
        reason = (
            f"BTC {delta:+.2f}$ breaks {favorite.upper()} · regime={regime} "
            f"(vol {short_vol:.3%} vs base {base_vol:.3%}) -> {action} {side.upper()} "
            f"@ {price:.0%}"
        )
        return TradeSignal(side=side, price=price, reason=reason)
