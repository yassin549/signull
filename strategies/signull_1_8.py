"""Signull 1.8 — buy whichever side the outcome prices favor at candle open."""

from __future__ import annotations

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal

STRATEGY_CLASS = "Signull18Strategy"


class Signull18Strategy(Strategy):
    """Enter the side the market favors on the first usable quote, hold to close.

    At candle open the outcome prices already lean one way (e.g. Up 0.51 vs
    Down 0.49).  This buys the favored side at that price and holds it to
    resolution — no model, no BTC delta.
    """

    meta = StrategyMeta(
        id="signull_1_8",
        name="Signull 1.8 (Open Favorite)",
        description=(
            "At candle open, buys whichever side the outcome prices favor "
            "(Up when P(Up) > P(Down), else Down) and holds to resolution. "
            "Risks a fixed fraction of capital (default 10%)."
        ),
        default_params={
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
        reason = f"Open favorite {side.upper()} ({up * 100:.1f}% vs {down * 100:.1f}%)"
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
