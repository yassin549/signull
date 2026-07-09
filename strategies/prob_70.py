"""Buy when Up or Down probability exceeds 70%, size by conviction."""

from __future__ import annotations

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal

STRATEGY_CLASS = "Prob70Strategy"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class Prob70Strategy(Strategy):
    meta = StrategyMeta(
        id="prob_70",
        name="70% Probability",
        description=(
            "Buy when Up or Down exceeds the threshold (one trade per candle). "
            "Stake scales from min→max % of initial capital based on how strong "
            "the probability is (weak 70% = small, strong 90%+ = big)."
        ),
        default_params={
            "threshold": 0.70,
            "min_risk_pct": 0.05,
            "max_risk_pct": 0.50,
            "scale_at": 0.92,
        },
    )

    def evaluate(self, tick: TickContext, candle: CandleContext, *, entered: bool) -> TradeSignal | None:
        if entered:
            return None

        threshold = float(self.params["threshold"])

        if tick.up >= threshold:
            return TradeSignal(
                side="up",
                price=tick.up,
                reason=f"Up {tick.up:.1%} ≥ {threshold:.0%}",
            )

        if tick.down >= threshold:
            return TradeSignal(
                side="down",
                price=tick.down,
                reason=f"Down {tick.down:.1%} ≥ {threshold:.0%}",
            )

        return None

    def position_risk_fraction(
        self,
        signal: TradeSignal,
        tick: TickContext,
        candle: CandleContext,
    ) -> float:
        """
        Scale stake between min and max using entry probability.

        At threshold → min_risk_pct (small).
        At scale_at (default 92%) or above → max_risk_pct (big).
        Linear between.
        """
        min_r = float(self.params["min_risk_pct"])
        max_r = float(self.params["max_risk_pct"])
        threshold = float(self.params["threshold"])
        scale_at = float(self.params.get("scale_at", 0.92))

        p = signal.price
        if scale_at <= threshold:
            return max_r

        t = _clamp((p - threshold) / (scale_at - threshold), 0.0, 1.0)
        return min_r + t * (max_r - min_r)

    def size_label(self, risk_frac: float) -> str:
        min_r = float(self.params["min_risk_pct"])
        max_r = float(self.params["max_risk_pct"])
        mid = (min_r + max_r) / 2
        if risk_frac <= min_r + (mid - min_r) * 0.35:
            return "small"
        if risk_frac >= max_r - (max_r - mid) * 0.35:
            return "big"
        return "medium"