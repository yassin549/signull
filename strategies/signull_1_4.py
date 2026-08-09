"""Signull 1.4 — fixed 10% risk on first side to reach 70¢."""

from __future__ import annotations

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


STRATEGY_CLASS = "Signull14Strategy"


class Signull14Strategy(Strategy):
    """Enters the first side reaching 70¢ per candle, risking a fixed 10% of capital."""

    meta = StrategyMeta(
        id="signull_1_4",
        name="Signull 1.4 (Fixed 10% @ 70¢)",
        description=(
            "Buys whichever option (UP or DOWN) reaches 70¢ first in each candle. "
            "Risks a fixed 10% of current capital on every trade."
        ),
        default_params={
            "threshold": 0.70,
            "risk_pct": 0.10,
        },
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._last_size_label = "fixed-10%"

    def evaluate(
        self, tick: TickContext, candle: CandleContext, *, entered: bool
    ) -> TradeSignal | None:
        if entered:
            return None
        threshold = float(self.params["threshold"])

        up_hit = tick.up >= threshold
        down_hit = tick.down >= threshold

        if not up_hit and not down_hit:
            return None

        if up_hit and down_hit:
            side = "up" if tick.up >= tick.down else "down"
            price = tick.up if side == "up" else tick.down
        elif up_hit:
            side, price = "up", tick.up
        else:
            side, price = "down", tick.down

        return TradeSignal(
            side=side,
            price=price,
            reason=f"{side.upper()} reached {threshold:.0%} first @ {price:.0%}",
        )

    def position_risk_fraction(
        self, signal: TradeSignal, tick: TickContext, candle: CandleContext
    ) -> float:
        del signal, tick, candle
        risk_fraction = float(self.params["risk_pct"])
        self._last_size_label = "fixed-10%"

        # Strictly risk fixed % (10%) of current equity on every trade regardless of drawdown/streaks
        return (self._equity * risk_fraction) / max(self._initial_capital, 1e-9)

    def size_label(self, risk_frac: float) -> str:
        return self._last_size_label
