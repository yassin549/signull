"""Signull 1.7 - adaptive BTC point-of-no-return target."""

from __future__ import annotations

import bisect
import math
from statistics import NormalDist, median

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal
from src.ml.btc_features import Kline, fetch_klines


STRATEGY_CLASS = "Signull17Strategy"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class Signull17Strategy(Strategy):
    """
    Enters the first side whose BTC move crosses a per-candle adaptive barrier.

    The barrier is calculated once, at the first usable tick for each 5-minute
    candle, from closed 1-minute BTC bars before that candle. It is then frozen
    for the candle so later volatility changes cannot move the entry target.
    """

    meta = StrategyMeta(
        id="signull_1_7",
        name="Signull 1.7 (Adaptive BTC Point-of-No-Return)",
        description=(
            "Buys whichever side first crosses a per-candle adaptive BTC dollar "
            "threshold. The threshold is frozen at candle open and is based on "
            "local volatility, recent range, choppiness, trend strength, and a "
            "target probability of returning back across the candle-open level."
        ),
        default_params={
            "risk_pct": 0.10,
            "asset": "btc",
            "lookback_minutes": 60,
            "target_reversal_prob": 0.35,
            "min_delta": 5.0,
            "max_delta": 250.0,
            "range_floor_multiplier": 0.65,
            "chop_multiplier": 0.20,
            "trend_discount": 0.30,
            "threshold_multiplier": 0.80,
            "fallback_delta": 30.0,
        },
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._last_size_label = "fixed-10%"
        self._asset = str(self.params.get("asset", "btc")).lower()
        self._candle_state: dict[str, dict[str, float]] = {}
        self._klines: list[Kline] = []
        self._kline_opens: list[int] = []

    def prepare_backtest(self, candles: list) -> None:
        self._candle_state.clear()
        if not candles:
            self._klines = []
            self._kline_opens = []
            return

        lookback = int(max(5, float(self.params.get("lookback_minutes", 60))))
        t_min = int(candles[0].start_ts - (lookback + 5) * 60)
        t_max = int(candles[-1].end_ts + 300)
        rows = fetch_klines(t_min, t_max, asset=self._asset) or []
        self._klines = sorted(rows, key=lambda x: x[0])
        self._kline_opens = [k[0] for k in self._klines]

    def _get_btc_price(self, tick: TickContext) -> float:
        if tick.btc_price is not None and tick.btc_price > 0:
            return float(tick.btc_price)

        if not self._klines or not self._kline_opens:
            return 0.0

        t_ms = int(tick.t * 1000)
        idx = bisect.bisect_right(self._kline_opens, t_ms) - 1
        if 0 <= idx < len(self._klines):
            k = self._klines[idx]
            if t_ms == k[0]:
                return float(k[1])
            return float(k[4])
        return 0.0

    def _history_before(self, candle_start_ts: int) -> list[Kline]:
        lookback = int(max(5, float(self.params.get("lookback_minutes", 60))))
        start_ms = int(candle_start_ts * 1000)
        idx = bisect.bisect_left(self._kline_opens, start_ms)
        bars = self._klines[max(0, idx - lookback - 1):idx]
        if len(bars) >= 2:
            return bars

        # Live fallback: if prepare_backtest was not called and live ticks only
        # provide the current BTC price, fetch enough closed 1m bars on demand.
        try:
            rows = fetch_klines(
                int(candle_start_ts - (lookback + 5) * 60),
                int(candle_start_ts),
                asset=self._asset,
            )
        except Exception:
            rows = []
        if rows:
            self._klines = sorted(rows, key=lambda x: x[0])
            self._kline_opens = [k[0] for k in self._klines]
            idx = bisect.bisect_left(self._kline_opens, start_ms)
            return self._klines[max(0, idx - lookback - 1):idx]
        return []

    def _dynamic_threshold(self, candle_start_ts: int, start_price: float) -> tuple[float, dict[str, float]]:
        fallback = float(self.params.get("fallback_delta", 30.0))
        min_delta = max(0.01, float(self.params.get("min_delta", 10.0)))
        max_delta = max(min_delta, float(self.params.get("max_delta", 250.0)))

        bars = self._history_before(candle_start_ts)
        if start_price <= 0 or len(bars) < 3:
            threshold = _clamp(fallback, min_delta, max_delta)
            return threshold, {
                "vol_delta": threshold,
                "range_floor": threshold,
                "chop_factor": 1.0,
                "trend_factor": 1.0,
                "reversal_prob": float(self.params.get("target_reversal_prob", 0.25)),
            }

        returns: list[float] = []
        ranges: list[float] = []
        for prev, cur in zip(bars, bars[1:]):
            prev_close = float(prev[4])
            close = float(cur[4])
            if prev_close > 0 and close > 0:
                returns.append(math.log(close / prev_close))
            high = float(cur[2])
            low = float(cur[3])
            if high >= low:
                ranges.append(high - low)

        if len(returns) < 2:
            threshold = _clamp(fallback, min_delta, max_delta)
            return threshold, {
                "vol_delta": threshold,
                "range_floor": threshold,
                "chop_factor": 1.0,
                "trend_factor": 1.0,
                "reversal_prob": float(self.params.get("target_reversal_prob", 0.25)),
            }

        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        sigma_1m = math.sqrt(max(0.0, variance))
        horizon_minutes = 5.0

        target_reversal = _clamp(
            float(self.params.get("target_reversal_prob", 0.25)),
            0.02,
            0.80,
        )
        # Reflection-principle approximation:
        # P(return to open from barrier d during horizon) ~= 2 * (1 - Phi(d/sigma)).
        z = NormalDist().inv_cdf(1.0 - target_reversal / 2.0)
        vol_delta = start_price * sigma_1m * math.sqrt(horizon_minutes) * z

        abs_path = sum(abs(r) for r in returns[-10:])
        net_move = abs(sum(returns[-10:]))
        chop = abs_path / max(net_move, 1e-9)
        chop_norm = _clamp((chop - 1.0) / 5.0, 0.0, 1.0)
        chop_factor = 1.0 + float(self.params.get("chop_multiplier", 0.35)) * chop_norm

        trend_strength = abs(mean_ret) / max(sigma_1m, 1e-9)
        trend_norm = _clamp(trend_strength / 2.0, 0.0, 1.0)
        trend_factor = 1.0 - float(self.params.get("trend_discount", 0.20)) * trend_norm

        range_floor = 0.0
        if ranges:
            range_floor = median(ranges[-min(len(ranges), 20):]) * float(
                self.params.get("range_floor_multiplier", 0.85)
            )

        multiplier = _clamp(float(self.params.get("threshold_multiplier", 0.80)), 0.25, 2.0)
        raw_threshold = max(vol_delta * chop_factor * trend_factor, range_floor) * multiplier
        threshold = _clamp(raw_threshold, min_delta, max_delta)
        return threshold, {
            "vol_delta": vol_delta,
            "range_floor": range_floor,
            "chop_factor": chop_factor,
            "trend_factor": trend_factor,
            "reversal_prob": target_reversal,
        }

    def _state_for(self, candle: CandleContext, start_price: float) -> dict[str, float]:
        st = self._candle_state.get(candle.slug)
        if st is not None:
            return st

        threshold, parts = self._dynamic_threshold(int(candle.start_ts), start_price)
        st = {
            "start_btc": float(start_price),
            "threshold": float(threshold),
            "vol_delta": float(parts["vol_delta"]),
            "range_floor": float(parts["range_floor"]),
            "chop_factor": float(parts["chop_factor"]),
            "trend_factor": float(parts["trend_factor"]),
            "reversal_prob": float(parts["reversal_prob"]),
            "signaled": 0.0,
        }
        self._candle_state[candle.slug] = st
        return st

    def evaluate(
        self,
        tick: TickContext,
        candle: CandleContext,
        *,
        entered: bool,
    ) -> TradeSignal | None:
        if entered:
            return None

        current_btc = self._get_btc_price(tick)
        if current_btc <= 0:
            return None

        st = self._state_for(candle, current_btc)
        if st["signaled"]:
            return None

        start_btc = st["start_btc"]
        threshold = st["threshold"]
        delta = current_btc - start_btc

        up_hit = delta >= threshold
        down_hit = delta <= -threshold
        if not up_hit and not down_hit:
            return None

        side = "up" if up_hit else "down"
        price = tick.up if side == "up" else tick.down
        st["signaled"] = 1.0

        return TradeSignal(
            side=side,
            price=price,
            reason=(
                f"BTC reached {delta:+.2f}$ vs adaptive PNR +/-${threshold:.2f} "
                f"(target return risk <= {st['reversal_prob']:.0%}, "
                f"vol=${st['vol_delta']:.2f}, range=${st['range_floor']:.2f}, "
                f"chop x{st['chop_factor']:.2f}, trend x{st['trend_factor']:.2f}) "
                f"-> buy {side.upper()} @ {price:.0%}"
            ),
        )

    def position_risk_fraction(
        self,
        signal: TradeSignal,
        tick: TickContext,
        candle: CandleContext,
    ) -> float:
        del signal, tick, candle
        risk_fraction = float(self.params.get("risk_pct", 0.10))
        self._last_size_label = f"fixed-{int(risk_fraction * 100)}%"
        return (self._equity * risk_fraction) / max(self._initial_capital, 1e-9)

    def size_label(self, risk_frac: float) -> str:
        del risk_frac
        return self._last_size_label
