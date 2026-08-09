"""The 2 Minutes — buy the higher-probability side with 2 minutes left."""

from __future__ import annotations

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal

STRATEGY_CLASS = "The2MinutesStrategy"


class The2MinutesStrategy(Strategy):
    """Wait until ~2 minutes remain, then buy whichever side leads."""

    meta = StrategyMeta(
        id="the_2_minutes",
        name="The 2 Minutes",
        description=(
            "After ~3 minutes into a 5-minute candle (2 minutes left to close), "
            "buy whichever option has the higher probability and hold to resolution. "
            "One trade per candle."
        ),
        default_params={
            # Enter once seconds remaining drops to this value (or below).
            "entry_seconds_left": 120.0,
            "risk_pct": 0.10,
        },
    )

    def evaluate(
        self, tick: TickContext, candle: CandleContext, *, entered: bool
    ) -> TradeSignal | None:
        if entered:
            return None

        entry_left = max(0.0, float(self.params["entry_seconds_left"]))
        # Still too early — wait until we hit the 2-minute mark.
        if tick.seconds_to_close > entry_left:
            return None

        side = "up" if tick.up >= tick.down else "down"
        price = tick.up if side == "up" else tick.down
        leader_pct = max(tick.up, tick.down)
        return TradeSignal(
            side=side,
            price=price,
            reason=(
                f"{side.upper()} leads @ {leader_pct:.1%} with "
                f"{tick.seconds_to_close:.0f}s left "
                f"(up={tick.up:.1%}, down={tick.down:.1%})"
            ),
        )

    def position_risk_fraction(
        self, signal: TradeSignal, tick: TickContext, candle: CandleContext
    ) -> float:
        del signal, tick, candle
        return float(self.params.get("risk_pct", 0.10))

    def size_label(self, risk_frac: float) -> str:
        del risk_frac
        return "flat"
