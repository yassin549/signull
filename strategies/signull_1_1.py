"""Signull 1.1 — online-calibrated fractional-Kelly sizing."""

from __future__ import annotations

import json
import math
from pathlib import Path

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal

STRATEGY_CLASS = "Signull11Strategy"
CALIBRATION_PATH = Path(__file__).resolve().parent.parent / "data" / "signull_1_1_calibration.json"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _wilson_lower_bound(wins: float, total: float, z: float) -> float:
    """Conservative lower confidence bound for a Bernoulli win rate."""
    if total <= 0:
        return 0.0
    p = wins / total
    z2 = z * z
    center = p + z2 / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z2 / (4 * total)) / total)
    return _clamp((center - spread) / (1 + z2 / total), 0.0, 1.0)


class Signull11Strategy(Strategy):
    """70¢ favorite entries with online calibration and fractional Kelly risk."""

    meta = StrategyMeta(
        id="signull_1_1",
        name="Signull 1.1 (Calibrated Kelly)",
        description=(
            "Favorite entries at 70¢. Learns its realised win rate online, sizes "
            "only a conservative lower-confidence edge with fractional Kelly, "
            "and uses small exploration stakes while it calibrates."
        ),
        default_params={
            "threshold": 0.70,
            "exploration_risk_pct": 0.10,
            "warmup_trades": 30,
            "prior_strength": 20.0,
            "confidence_z": 1.64,
            "edge_buffer": 0.015,
            "kelly_fraction": 0.15,
            "max_risk_pct": 0.10,
            "drawdown_stop_pct": 0.15,
            "min_drawdown_multiplier": 0.25,
            "persist_calibration": False,
        },
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._stats: dict[str, list[int]] = {"all": [0, 0]}
        self._pending_bucket: str | None = None
        self._last_size_label = "explore"
        self._calibration_path = Path(
            self.params.get("calibration_path", CALIBRATION_PATH)
        )
        if self.params.get("persist_calibration"):
            self._load_calibration()

    def _load_calibration(self) -> None:
        try:
            payload = json.loads(self._calibration_path.read_text(encoding="utf-8"))
            if abs(float(payload.get("threshold")) - float(self.params["threshold"])) > 1e-9:
                return
            stats = payload.get("stats", {})
            loaded = {
                str(key): [max(0, int(value[0])), max(0, int(value[1]))]
                for key, value in stats.items()
                if isinstance(value, list) and len(value) == 2
            }
            if "all" in loaded:
                self._stats = loaded
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # Starting cold is safe; a missing or malformed calibration file
            # must never stop the live strategy.
            return

    def _save_calibration(self) -> None:
        if not self.params.get("persist_calibration"):
            return
        try:
            self._calibration_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "threshold": float(self.params["threshold"]),
                "stats": self._stats,
            }
            temporary = self._calibration_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(self._calibration_path)
        except OSError:
            return

    def _bucket(self, seconds_in: float) -> str:
        if seconds_in < 60:
            return "early"
        if seconds_in < 180:
            return "mid"
        return "late"

    def _posterior_lower(self, bucket: str, market_price: float) -> tuple[float, int]:
        wins, total = self._stats.get(bucket, [0, 0])
        # Use the global sample until the time-of-entry bucket has enough data.
        if bucket != "all" and total < 15:
            wins, total = self._stats["all"]
        prior = max(0.0, float(self.params["prior_strength"]))
        effective_wins = wins + prior * market_price
        effective_total = total + prior
        return _wilson_lower_bound(
            effective_wins,
            effective_total,
            max(0.0, float(self.params["confidence_z"])),
        ), total

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
        return None

    def position_risk_fraction(
        self, signal: TradeSignal, tick: TickContext, candle: CandleContext
    ) -> float:
        del candle
        price = _clamp(float(signal.price), 0.01, 0.99)
        bucket = self._bucket(tick.seconds_into_candle)
        lower_q, samples = self._posterior_lower(bucket, price)
        self._pending_bucket = bucket

        if samples < int(self.params["warmup_trades"]):
            current_fraction = float(self.params["exploration_risk_pct"])
            self._last_size_label = "explore"
            signal.reason += f" · calibration {samples}/{int(self.params['warmup_trades'])}"
        else:
            edge = lower_q - price - float(self.params["edge_buffer"])
            if edge <= 0:
                self._last_size_label = "no-edge"
                signal.reason += f" · no conservative edge (qL {lower_q:.1%})"
                return 0.0

            full_kelly = edge / (1 - price)
            current_fraction = float(self.params["kelly_fraction"]) * full_kelly
            peak = max(self._peak_equity, 1e-9)
            drawdown = max(0.0, (peak - self._equity) / peak)
            stop = max(1e-9, float(self.params["drawdown_stop_pct"]))
            floor = _clamp(float(self.params["min_drawdown_multiplier"]), 0.0, 1.0)
            drawdown_multiplier = max(floor, 1 - drawdown / stop)
            current_fraction *= drawdown_multiplier
            current_fraction = _clamp(
                current_fraction, 0.0, float(self.params["max_risk_pct"])
            )
            self._last_size_label = "kelly"
            signal.reason += (
                f" · qL {lower_q:.1%}, edge {edge:.1%}, "
                f"Kelly {current_fraction:.1%} of bankroll"
            )

        # The engine accepts risk as a fraction of initial capital. Convert a
        # current-bankroll allocation so the intended stake remains proportional
        # to current equity as the bankroll changes.
        return (self._equity * current_fraction) / max(self._initial_capital, 1e-9)

    def on_trade_settled(self, won: bool) -> None:
        self._record_outcome(won)

    def on_signal_resolved(self, won: bool, *, traded: bool) -> None:
        # Executed trades are already recorded by on_trade_settled.  A
        # declined signal is still resolved in a backtest, so record it too:
        # otherwise one no-edge decision freezes calibration permanently.
        if not traded:
            self._record_outcome(won)

    def _record_outcome(self, won: bool) -> None:
        bucket = self._pending_bucket
        if bucket is None:
            return
        for key in ("all", bucket):
            stats = self._stats.setdefault(key, [0, 0])
            stats[0] += int(won)
            stats[1] += 1
        self._pending_bucket = None
        self._save_calibration()

    def size_label(self, risk_frac: float) -> str:
        return self._last_size_label
