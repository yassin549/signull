"""Signull 1.1 — fixed-stake favorite entries on every candle."""

from __future__ import annotations

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


STRATEGY_CLASS = "Signull11Strategy"


class Signull11Strategy(Strategy):
    """Enter every candle: 70¢ threshold when available, else end-of-candle favorite."""

    meta = StrategyMeta(
        id="signull_1_1",
        name="Signull 1.1 (Always In)",
        description=(
            "Trades every candle. Enters at 70¢ when a side first crosses the "
            "threshold; otherwise buys the leading side near candle close. "
            "Uses a fixed bankroll fraction with drawdown throttling."
        ),
        default_params={
            "threshold": 0.70,
            "risk_pct": 0.10,
            "late_entry_seconds": 2.0,
            "drawdown_stop_pct": 0.15,
            "min_drawdown_multiplier": 0.25,
        },
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._last_size_label = "flat"

    def evaluate(
        self, tick: TickContext, candle: CandleContext, *, entered: bool
    ) -> TradeSignal | None:
        if entered:
            return None
        threshold = float(self.params["threshold"])
        if tick.up >= threshold:
            return TradeSignal("up", threshold, f"UP favorite ≥ {threshold:.0%}")
        if tick.down >= threshold:
            return TradeSignal("down", threshold, f"DOWN favorite ≥ {threshold:.0%}")

        late_cutoff = max(0.0, float(self.params["late_entry_seconds"]))
        if tick.seconds_to_close > late_cutoff:
            return None

        side = "up" if tick.up >= tick.down else "down"
        price = tick.up if side == "up" else tick.down
        return TradeSignal(
            side,
            price,
            f"{side.upper()} end-of-candle entry @ {price:.0%}",
        )

    def position_risk_fraction(
        self, signal: TradeSignal, tick: TickContext, candle: CandleContext
    ) -> float:
        del signal, tick, candle
        current_fraction = float(self.params["risk_pct"])
        peak = max(self._peak_equity, 1e-9)
        drawdown = max(0.0, (peak - self._equity) / peak)
        stop = max(1e-9, float(self.params["drawdown_stop_pct"]))
        floor = _clamp(float(self.params["min_drawdown_multiplier"]), 0.0, 1.0)
        drawdown_multiplier = max(floor, 1 - drawdown / stop)
        current_fraction *= drawdown_multiplier
        self._last_size_label = "flat"

        # The engine accepts risk as a fraction of initial capital. Convert a
        # current-bankroll allocation so the intended stake remains proportional
        # to current equity as the bankroll changes.
        return (self._equity * current_fraction) / max(self._initial_capital, 1e-9)

    def size_label(self, risk_frac: float) -> str:
        return self._last_size_label