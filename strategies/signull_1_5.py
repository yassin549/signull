"""Signull 1.5 — buys whichever side reaches + or - $30 on the Bitcoin chart."""

from __future__ import annotations

import bisect
from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal
from src.ml.btc_features import fetch_klines, Kline


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


STRATEGY_CLASS = "Signull15Strategy"


class Signull15Strategy(Strategy):
    """Enters whichever option (UP or DOWN) first reaches +$30 or -$30 change on the BTC chart."""

    meta = StrategyMeta(
        id="signull_1_5",
        name="Signull 1.5 (BTC ±$30 Target)",
        description=(
            "Buys whichever side (UP or DOWN) reaches +$30 or -$30 change "
            "on the Bitcoin chart relative to the candle start price. "
            "Risks a fixed fraction of capital (default 10%)."
        ),
        default_params={
            "target_delta": 30.0,
            "risk_pct": 0.10,
            "asset": "btc",
        },
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._last_size_label = "fixed-10%"
        self._asset = str(self.params.get("asset", "btc")).lower()
        self._candle_start_btc: dict[str, float] = {}
        self._klines: list[Kline] = []
        self._kline_opens: list[int] = []

    def prepare_backtest(self, candles: list) -> None:
        self._candle_start_btc.clear()
        if not candles:
            self._klines = []
            self._kline_opens = []
            return
        t_min = candles[0].start_ts - 300
        t_max = candles[-1].end_ts + 300
        rows = fetch_klines(t_min, t_max, asset=self._asset) or []
        self._klines = sorted(rows, key=lambda x: x[0])
        self._kline_opens = [k[0] for k in self._klines]

    def _get_btc_price(self, tick: TickContext, candle: CandleContext) -> float:
        if tick.btc_price is not None and tick.btc_price > 0:
            return tick.btc_price

        if not self._klines or not self._kline_opens:
            return 0.0

        t_ms = tick.t * 1000
        idx = bisect.bisect_right(self._kline_opens, t_ms) - 1
        if idx >= 0 and idx < len(self._klines):
            k = self._klines[idx]
            # (open_time_ms, open, high, low, close, volume)
            if t_ms == k[0]:
                return float(k[1])  # open price at start of bar
            return float(k[4])  # close price

        return 0.0

    def evaluate(
        self, tick: TickContext, candle: CandleContext, *, entered: bool
    ) -> TradeSignal | None:
        if entered:
            return None

        current_btc = self._get_btc_price(tick, candle)
        if current_btc <= 0:
            return None

        if candle.slug not in self._candle_start_btc:
            self._candle_start_btc[candle.slug] = current_btc

        start_btc = self._candle_start_btc[candle.slug]
        delta = current_btc - start_btc
        target_delta = float(self.params.get("target_delta", 30.0))

        up_hit = delta >= target_delta
        down_hit = delta <= -target_delta

        if not up_hit and not down_hit:
            return None

        if up_hit and down_hit:
            side = "up" if delta >= 0 else "down"
        elif up_hit:
            side = "up"
        else:
            side = "down"

        price = tick.up if side == "up" else tick.down
        reason = (
            f"BTC reached {delta:+.2f}$ (target ±${target_delta:.2f}) "
            f"-> buy {side.upper()} @ {price:.0%}"
        )

        return TradeSignal(
            side=side,
            price=price,
            reason=reason,
        )

    def position_risk_fraction(
        self, signal: TradeSignal, tick: TickContext, candle: CandleContext
    ) -> float:
        del signal, tick, candle
        risk_fraction = float(self.params.get("risk_pct", 0.10))
        self._last_size_label = f"fixed-{int(risk_fraction * 100)}%"
        return (self._equity * risk_fraction) / max(self._initial_capital, 1e-9)

    def size_label(self, risk_frac: float) -> str:
        del risk_frac
        return self._last_size_label
