"""Fixed 70% entry; TCN sizes the bet from BTC chart + equity curve."""

from __future__ import annotations

import numpy as np

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal

STRATEGY_CLASS = "TCNSizerStrategy"


def _equity_features(
    equity: float,
    initial: float,
    peak: float,
    wins_recent: int,
    losses_streak: int,
    equity_momentum: float = 0.0,
) -> np.ndarray:
    dd = (peak - equity) / peak if peak > 0 else 0.0
    return np.array([
        equity / initial if initial > 0 else 1.0,
        1.0 - min(1.0, dd),
        wins_recent / 10.0,
        min(losses_streak, 5) / 5.0,
        np.log(max(equity, 1.0) / initial) if initial > 0 else 0.0,
        float(np.tanh(equity_momentum / initial)) if initial > 0 else 0.0,
    ], dtype=np.float32)


class TCNSizerStrategy(Strategy):
    meta = StrategyMeta(
        id="tcn_sizer",
        name="TCN Sizer (70% entry)",
        description=(
            "Entry is fixed: buy when Up or Down hits 70%. "
            "A trained TCN predicts stake size (5–50% of initial capital) "
            "from the BTC 1m chart and current equity curve — not from probability."
        ),
        default_params={
            "threshold": 0.70,
            "min_risk_pct": 0.05,
            "max_risk_pct": 0.50,
        },
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._predictor = None
        self._last_entry_ts: int | None = None

    def _get_predictor(self):
        if self._predictor is None:
            from src.ml.predictor import TCNSizerPredictor
            self._predictor = TCNSizerPredictor()
        return self._predictor

    def prepare_backtest(self, candles) -> None:
        """Preload BTC klines for fast inference."""
        if not candles:
            return
        pred = self._get_predictor()
        t_min = candles[0].start_ts - pred.lookback * 60 - 120
        t_max = candles[-1].end_ts + 60
        pred.preload(t_min, t_max)

    def evaluate(self, tick: TickContext, candle: CandleContext, *, entered: bool) -> TradeSignal | None:
        if entered:
            return None

        threshold = float(self.params["threshold"])

        if tick.up >= threshold:
            self._last_entry_ts = tick.t
            return TradeSignal(
                side="up",
                price=tick.up,
                reason=f"Up {tick.up:.1%} ≥ {threshold:.0%} (TCN sizes)",
            )

        if tick.down >= threshold:
            self._last_entry_ts = tick.t
            return TradeSignal(
                side="down",
                price=tick.down,
                reason=f"Down {tick.down:.1%} ≥ {threshold:.0%} (TCN sizes)",
            )

        return None

    def position_risk_fraction(
        self,
        signal: TradeSignal,
        tick: TickContext,
        candle: CandleContext,
    ) -> float:
        eq = _equity_features(
            self._equity,
            self._initial_capital,
            self._peak_equity,
            self._wins_recent,
            self._losses_streak,
            self._equity_momentum,
        )
        try:
            pred = self._get_predictor()
            risk = pred.predict(tick.t, eq, side=signal.side)
        except FileNotFoundError:
            risk = float(self.params["min_risk_pct"])

        lo = float(self.params["min_risk_pct"])
        hi = float(self.params["max_risk_pct"])
        return float(max(lo, min(hi, risk)))

    def size_label(self, risk_frac: float) -> str:
        lo = float(self.params["min_risk_pct"])
        hi = float(self.params["max_risk_pct"])
        mid = (lo + hi) / 2
        if risk_frac <= lo + (mid - lo) * 0.35:
            return "small"
        if risk_frac >= hi - (hi - mid) * 0.35:
            return "big"
        return "medium"