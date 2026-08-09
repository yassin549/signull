"""
Blind model-threshold strategy (v1).

Uses the sim BTC 5m direction TCN — NOT Polymarket crowd prices — to decide
side. When model P(Up) or P(Down) first reaches `threshold`, buy that side.

At backtest start the strategy:
  1. Downloads/caches Binance BTCUSDT 1s bars for the requested window
  2. Builds the same causal features the model was trained on
  3. Runs the TCN to get P(Up) for every second of every candle

So the model can score *any* historical 5m candle that has public 1s data —
not only the original two-week training export.
"""

from __future__ import annotations

import logging
from typing import Any

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal

STRATEGY_CLASS = "ModelProbThresholdStrategy"

logger = logging.getLogger(__name__)


def _as_unit_interval(value: float, name: str) -> float:
    """Accept 0.70 or 70 for percentage-like params."""
    v = float(value)
    if v > 1.0:
        v = v / 100.0
    if not (0.0 < v < 1.0):
        raise ValueError(f"{name} must be in (0, 1) or (0, 100], got {value}")
    return v


class ModelProbThresholdStrategy(Strategy):
    meta = StrategyMeta(
        id="model_prob_threshold",
        name="Model Prob Threshold (TCN)",
        description=(
            "Direction TCN estimates P(candle closes Up) every second from BTC 1s "
            "data (not Polymarket crowd prices). Buys the first side whose model "
            "probability crosses `threshold`. Fill uses the market mid at the "
            "signal tick. "
            "On each backtest the model runs live inference over your date range "
            "(downloads Binance 1s as needed). "
            "threshold is a fraction (0.70), not 70. "
            "For honest evaluation prefer dates the model was not trained on."
        ),
        default_params={
            "threshold": 0.70,
            "risk_pct": 0.10,
            "min_seconds_elapsed": 0,
            "max_seconds_elapsed": 299,
            "taker_fee_rate": 0.0,
        },
    )

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        self._store = None
        self._candle_state: dict[str, Any] = {}
        self.last_coverage: dict[str, Any] | None = None
        self._progress_callback = None
        try:
            self.params["threshold"] = _as_unit_interval(
                self.params["threshold"], "threshold"
            )
        except (TypeError, ValueError):
            self.params["threshold"] = 0.70
        try:
            self.params["risk_pct"] = _as_unit_interval(
                self.params["risk_pct"], "risk_pct"
            )
        except (TypeError, ValueError):
            self.params["risk_pct"] = 0.10

    def prepare_backtest(self, candles, progress_callback=None) -> None:
        """
        Score every candle in the run with the direction TCN.

        Downloads BTC 1s history as needed, builds causal features, runs the
        network — this is actual model inference, not a frozen lookup table.
        """
        if not candles:
            raise ValueError("No candles supplied to model_prob_threshold")

        # Engine currently calls prepare_backtest(candles) only; accept optional cb.
        cb = progress_callback or self._progress_callback

        try:
            from src.ml.direction_inference import build_store_for_candles

            self._store = build_store_for_candles(
                candles,
                progress_callback=cb,
                use_precomputed_when_available=True,
            )
        except Exception as exc:
            raise ValueError(
                f"Direction model inference failed: {type(exc).__name__}: {exc}"
            ) from exc

        starts = [int(c.start_ts) for c in candles]
        cov = self._store.coverage(starts)
        lo, hi = self._store.time_range_iso()
        cov["model_range"] = f"{lo} → {hi}"
        cov["model_first_ts"] = self._store.first_ts
        cov["model_last_ts"] = self._store.last_ts
        cov["backtest_first_ts"] = starts[0]
        cov["backtest_last_ts"] = starts[-1]
        cov["inference"] = getattr(self._store, "_last_infer_meta", {})
        self.last_coverage = cov

        logger.info(
            "model_prob_threshold ready: coverage %s/%s (%.1f%%) source=%s",
            cov["candles_with_probs"],
            cov["candles_requested"],
            100.0 * cov["coverage"],
            self._store.source,
        )

        if cov["candles_with_probs"] <= 0:
            raise ValueError(
                "Direction TCN produced probabilities for 0 of "
                f"{cov['candles_requested']} backtest candles. "
                "Binance 1s data may be missing for this range (too recent / unpublished), "
                "or candle timestamps did not align to 5-minute boundaries."
            )

        self._candle_state = {}

    def _get_store(self):
        if self._store is None:
            raise RuntimeError(
                "Direction store not built. prepare_backtest() must run first."
            )
        return self._store

    def _state_for(self, slug: str) -> dict[str, Any]:
        st = self._candle_state.get(slug)
        if st is None:
            st = {
                "signaled": False,
                "missing_probs": False,
                "cross_pending": None,
            }
            self._candle_state[slug] = st
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

        st = self._state_for(candle.slug)
        if st["signaled"]:
            return None

        store = self._get_store()
        if not store.has_candle(int(candle.start_ts)):
            st["missing_probs"] = True
            return None

        threshold = float(self.params["threshold"])
        min_s = int(self.params.get("min_seconds_elapsed", 0))
        max_s = int(self.params.get("max_seconds_elapsed", 299))
        fee = float(self.params.get("taker_fee_rate", 0.0))

        elapsed = int(max(0.0, float(tick.t) - float(candle.start_ts)))
        up_to = min(elapsed, max_s)
        if up_to < min_s and st["cross_pending"] is None:
            return None

        if st["cross_pending"] is None:
            cross = store.first_threshold_cross(
                int(candle.start_ts),
                threshold,
                up_to_elapsed=up_to,
            )
            if cross is None:
                return None
            side, cross_sec, model_p = cross
            if cross_sec < min_s:
                return None
            st["cross_pending"] = (side, cross_sec, model_p)
        else:
            side, cross_sec, model_p = st["cross_pending"]

        market_px = float(tick.up if side == "up" else tick.down)
        if market_px <= 0.01 or market_px >= 0.99:
            return None

        st["signaled"] = True
        st["cross_pending"] = None
        return TradeSignal(
            side=side,
            price=market_px,
            reason=(
                f"model {side.upper()} {model_p:.1%} ≥ {threshold:.0%} "
                f"@ t+{cross_sec}s (fill mkt {market_px:.0%})"
            ),
            taker_fee_rate=max(0.0, fee),
        )

    def position_risk_fraction(
        self,
        signal: TradeSignal,
        tick: TickContext,
        candle: CandleContext,
    ) -> float:
        del signal, tick, candle
        return float(self.params["risk_pct"])

    def size_label(self, risk_frac: float) -> str:
        del risk_frac
        thr = float(self.params["threshold"])
        return f"model≥{thr:.0%}"
