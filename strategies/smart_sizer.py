"""Fixed 70% entry; binary big (50%) or small (5%) from conviction score."""

from __future__ import annotations

import numpy as np

from src.ml.btc_features import btc_momentum_align, fetch_klines, window_features
from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal

STRATEGY_CLASS = "SmartSizerStrategy"

LOOKBACK = 60


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class SmartSizerStrategy(Strategy):
    """
    Entry: first tick where Up or Down >= 70%.

    Binary sizing only — 5% (small) or 50% (big). A conviction score
    (probability + BTC + streaks) is computed, then snapped: score at
    or above threshold → big, otherwise small.
    """

    meta = StrategyMeta(
        id="smart_sizer",
        name="Smart Sizer (70% entry)",
        description=(
            "Fixed 70% entry. Binary sizing: 5% (small) or 50% (big) of initial "
            "capital — no in-between. A conviction score blends entry strength, "
            "BTC alignment, hot streaks, and drawdown; score ≥ threshold → big."
        ),
        default_params={
            "threshold": 0.70,
            "min_risk_pct": 0.05,
            "max_risk_pct": 0.50,
            "big_score_threshold": 0.50,
            "hot_wins": 5,
        },
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._klines: list | None = None
        self._last_size_label: str = "small"

    def prepare_backtest(self, candles) -> None:
        if not candles:
            return
        t_min = candles[0].start_ts - LOOKBACK * 60 - 120
        t_max = candles[-1].end_ts + 60
        self._klines = fetch_klines(t_min, t_max)

    def _btc_context(self, entry_ts: int, side: str) -> tuple[float, float]:
        if self._klines is None:
            self._klines = fetch_klines(entry_ts - LOOKBACK * 60 - 120, entry_ts + 60)
        feats = window_features(self._klines, entry_ts, lookback=LOOKBACK)
        if feats is None:
            return 0.5, 0.0
        align = btc_momentum_align(feats, side)
        vol = float(np.std(feats[:, 0]))
        return align, vol

    def _conviction_score(
        self,
        entry_price: float,
        *,
        btc_align: float,
        btc_vol: float,
        drawdown: float,
        losses_streak: int,
        wins_recent: int,
        seconds_in: float,
    ) -> float:
        threshold = float(self.params["threshold"])
        t = _clamp((entry_price - threshold) / 0.20, 0.0, 1.0)
        score = t

        if btc_align >= 0.55:
            score += 0.15
        elif btc_align < 0.45:
            score -= 0.20

        if btc_vol >= 0.0020:
            score -= 0.15

        if drawdown >= 0.20:
            score -= 0.20

        if losses_streak >= 2:
            score -= 0.15
        elif losses_streak >= 1:
            score -= 0.08

        if seconds_in < 60:
            score += 0.05

        hot_wins = int(self.params.get("hot_wins", 5))
        if wins_recent >= hot_wins:
            score += 0.15
        if wins_recent >= hot_wins + 3:
            score += 0.10

        return _clamp(score, 0.0, 1.0)

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
        dd = (
            (self._peak_equity - self._equity) / self._peak_equity
            if self._peak_equity > 0
            else 0.0
        )
        align, vol = self._btc_context(tick.t, signal.side)
        score = self._conviction_score(
            signal.price,
            btc_align=align,
            btc_vol=vol,
            drawdown=dd,
            losses_streak=self._losses_streak,
            wins_recent=self._wins_recent,
            seconds_in=tick.seconds_into_candle,
        )

        big_th = float(self.params["big_score_threshold"])
        # Never go big right after a loss — one small bet to reset
        go_big = self._losses_streak == 0 and score >= big_th

        lo = float(self.params["min_risk_pct"])
        hi = float(self.params["max_risk_pct"])
        self._last_size_label = "big" if go_big else "small"
        return hi if go_big else lo

    def size_label(self, risk_frac: float) -> str:
        return self._last_size_label