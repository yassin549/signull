"""Signull 1.9 — buy the side favored by the odds 3 minutes into the candle."""

from __future__ import annotations

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal

STRATEGY_CLASS = "Signull19Strategy"


class Signull19Strategy(Strategy):
    """Wait three minutes, then buy whichever side the outcome prices favor.

    Nothing happens for the first ``entry_seconds`` of the 5-minute candle.
    Once that mark passes, the first quote that actually leans one way is
    bought and held to resolution — one trade per candle.
    """

    meta = StrategyMeta(
        id="signull_1_9",
        name="Signull 1.9 (3-Minute Favorite)",
        description=(
            "Waits 3 minutes into the candle, then buys whichever side the "
            "outcome prices favor (Up when P(Up) > P(Down), else Down) and "
            "holds to resolution. Risks a fixed fraction of capital (default 10%)."
        ),
        default_params={
            "entry_seconds": 180.0,
            "risk_pct": 0.10,
            "min_edge": 0.0,
            "asset": "btc",
        },
    )

    def evaluate(
        self, tick: TickContext, candle: CandleContext, *, entered: bool
    ) -> TradeSignal | None:
        del candle
        if entered:
            return None

        entry_seconds = max(0.0, float(self.params.get("entry_seconds", 180.0)))
        if tick.seconds_into_candle < entry_seconds:
            return None

        up = float(tick.up)
        down = float(tick.down)
        if not (0.0 < up < 1.0 and 0.0 < down < 1.0):
            return None

        edge = up - down
        if edge == 0.0:
            # Dead heat — wait for the first quote that actually leans.
            return None

        min_edge = float(self.params.get("min_edge", 0.0))
        if abs(edge) < min_edge:
            return None

        side = "up" if edge > 0 else "down"
        price = up if side == "up" else down
        reason = (
            f"3-min favorite {side.upper()} ({up * 100:.1f}% vs {down * 100:.1f}%) "
            f"at +{tick.seconds_into_candle:.0f}s"
        )
        return TradeSignal(side=side, price=price, reason=reason)

    def position_risk_fraction(
        self, signal: TradeSignal, tick: TickContext, candle: CandleContext
    ) -> float:
        del signal, tick, candle
        return (self._equity * float(self.params.get("risk_pct", 0.10))) / max(
            self._initial_capital, 1e-9
        )

    def size_label(self, risk_frac: float) -> str:
        del risk_frac
        return f"fixed-{int(float(self.params.get('risk_pct', 0.10)) * 100)}%"
