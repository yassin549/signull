"""Signull 1.2 — regime-calibrated, fee-aware fractional Kelly sizing.

The entry thesis stays deliberately narrow: buy the first observed side at or
above the favourite threshold in every eligible candle. The sizing thesis is different from Signull 1.0:
past wins do not make the next position larger.  Instead, 1.2 maintains
shrunk outcome estimates for entry-price and entry-time regimes, then risks
capital only when a conservative lower confidence bound clears the executable
all-in cost (price plus estimated taker fee).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal

STRATEGY_CLASS = "Signull12Strategy"
CALIBRATION_PATH = Path(__file__).resolve().parent.parent / "data" / "signull_1_2_calibration.json"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _wilson_lower_bound(wins: float, total: float, z: float) -> float:
    """Conservative lower confidence bound for a Bernoulli win probability."""
    if total <= 0:
        return 0.0
    p = wins / total
    z2 = z * z
    center = p + z2 / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z2 / (4 * total)) / total)
    return _clamp((center - spread) / (1 + z2 / total), 0.0, 1.0)


class Signull12Strategy(Strategy):
    """Every threshold-eligible candle, sized from conservative regime edge."""

    meta = StrategyMeta(
        id="signull_1_2",
        name="Signull 1.2 (Regime Kelly)",
        description=(
            "First observed side at or above 70¢ enters at its observed/live "
            "executable price in every eligible candle. "
            "Learns price-and-time regimes online, requires a lower-confidence "
            "edge over price plus taker fees, and uses capped fractional Kelly. "
            "Win streaks never increase size."
        ),
        default_params={
            "threshold": 0.70,
            "exploration_risk_pct": 0.02,
            "warmup_trades": 50,
            "prior_strength": 30.0,
            "context_min_samples": 20,
            "context_shrink": 30.0,
            "confidence_z": 1.64,
            "edge_buffer": 0.015,
            "taker_fee_rate": 0.07,
            "kelly_fraction": 0.15,
            "max_risk_pct": 0.10,
            "drawdown_stop_pct": 0.15,
            "min_drawdown_multiplier": 0.25,
            "persist_calibration": False,
        },
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._normalise_params()
        self._stats: dict[str, list[int]] = {"all": [0, 0]}
        self._pending_keys: tuple[str, ...] | None = None
        self._last_size_label = "explore"
        self._calibration_path = Path(
            self.params.get("calibration_path", CALIBRATION_PATH)
        )
        if self.params["persist_calibration"]:
            self._load_calibration()

    def _normalise_params(self) -> None:
        p = self.params
        defaults = self.meta.default_params
        for key in (
            "threshold", "exploration_risk_pct", "prior_strength",
            "context_shrink", "confidence_z", "edge_buffer", "taker_fee_rate",
            "kelly_fraction", "max_risk_pct", "drawdown_stop_pct",
            "min_drawdown_multiplier",
        ):
            try:
                value = float(p.get(key, defaults[key]))
            except (TypeError, ValueError):
                value = float(defaults[key])
            p[key] = float(defaults[key]) if value != value else value

        if not 0.50 <= p["threshold"] < 0.99:
            p["threshold"] = float(defaults["threshold"])
        for key in (
            "exploration_risk_pct", "edge_buffer", "taker_fee_rate",
            "kelly_fraction", "max_risk_pct", "drawdown_stop_pct",
            "min_drawdown_multiplier",
        ):
            p[key] = _clamp(p[key], 0.0, 1.0)
        p["prior_strength"] = max(0.0, p["prior_strength"])
        p["context_shrink"] = max(0.0, p["context_shrink"])
        p["confidence_z"] = max(0.0, p["confidence_z"])
        for key in ("warmup_trades", "context_min_samples"):
            try:
                p[key] = max(1, int(float(p.get(key, defaults[key]))))
            except (TypeError, ValueError):
                p[key] = int(defaults[key])
        p["persist_calibration"] = bool(p.get("persist_calibration", False))

    def _load_calibration(self) -> None:
        try:
            payload = json.loads(self._calibration_path.read_text(encoding="utf-8"))
            if abs(float(payload.get("threshold")) - self.params["threshold"]) > 1e-9:
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
            return

    def _save_calibration(self) -> None:
        if not self.params["persist_calibration"]:
            return
        try:
            self._calibration_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"threshold": self.params["threshold"], "stats": self._stats}
            temporary = self._calibration_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(self._calibration_path)
        except OSError:
            return

    @staticmethod
    def _price_bucket(price: float) -> str:
        if price < 0.75:
            return "p70_74"
        if price < 0.80:
            return "p75_79"
        if price < 0.85:
            return "p80_84"
        return "p85_plus"

    @staticmethod
    def _time_bucket(seconds_in: float) -> str:
        if seconds_in < 60:
            return "early"
        if seconds_in < 180:
            return "mid"
        return "late"

    def _regime_keys(self, price: float, seconds_in: float) -> tuple[str, ...]:
        price_key = f"price:{self._price_bucket(price)}"
        time_key = f"time:{self._time_bucket(seconds_in)}"
        return ("all", f"regime:{price_key}|{time_key}", price_key, time_key)

    def _posterior_lower(
        self, keys: tuple[str, ...], market_price: float
    ) -> tuple[float, int, str]:
        """Return a globally shrunk lower bound and the selected regime sample."""
        global_wins, global_total = self._stats["all"]
        prior = self.params["prior_strength"]
        global_mean = (global_wins + prior * market_price) / max(
            global_total + prior, 1e-9
        )

        chosen = "all"
        chosen_wins, chosen_total = global_wins, global_total
        minimum = self.params["context_min_samples"]
        # Prefer the most specific sufficiently sampled regime, then price,
        # then time, before falling back to the global calibration.
        for key in keys[1:]:
            wins, total = self._stats.get(key, [0, 0])
            if total >= minimum:
                chosen, chosen_wins, chosen_total = key, wins, total
                break

        if chosen == "all":
            effective_wins = global_wins + prior * market_price
            effective_total = global_total + prior
        else:
            shrink = self.params["context_shrink"]
            effective_wins = chosen_wins + shrink * global_mean
            effective_total = chosen_total + shrink

        return (
            _wilson_lower_bound(
                effective_wins, effective_total, self.params["confidence_z"]
            ),
            chosen_total,
            chosen,
        )

    def evaluate(
        self, tick: TickContext, candle: CandleContext, *, entered: bool
    ) -> TradeSignal | None:
        if entered:
            return None

        threshold = self.params["threshold"]
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
            reason=(
                f"{side.upper()} observed at {price:.1%} ≥ {threshold:.0%}; "
                "regime Kelly pending"
            ),
            taker_fee_rate=self.params["taker_fee_rate"],
        )

    def position_risk_fraction(
        self, signal: TradeSignal, tick: TickContext, candle: CandleContext
    ) -> float:
        del candle
        price = _clamp(float(signal.price), 0.01, 0.99)
        keys = self._regime_keys(price, tick.seconds_into_candle)
        lower_q, samples, regime = self._posterior_lower(keys, price)
        self._pending_keys = keys

        if self._stats["all"][1] < self.params["warmup_trades"]:
            current_fraction = self.params["exploration_risk_pct"]
            self._last_size_label = "explore"
            signal.reason += (
                f" · exploration {self._stats['all'][1]}/"
                f"{self.params['warmup_trades']}"
            )
        else:
            fee_per_share = price * signal.taker_fee_rate * (1.0 - price)
            all_in_cost = _clamp(price + fee_per_share, 0.01, 0.999)
            edge = lower_q - all_in_cost - self.params["edge_buffer"]
            if edge <= 0:
                self._last_size_label = "no-edge"
                signal.reason += (
                    f" · no edge: qL {lower_q:.1%} ≤ cost {all_in_cost:.1%}"
                )
                return 0.0

            full_kelly = edge / (1.0 - all_in_cost)
            current_fraction = self.params["kelly_fraction"] * full_kelly
            peak = max(self._peak_equity, 1e-9)
            drawdown = max(0.0, (peak - self._equity) / peak)
            stop = max(self.params["drawdown_stop_pct"], 1e-9)
            floor = self.params["min_drawdown_multiplier"]
            drawdown_multiplier = max(floor, 1.0 - drawdown / stop)
            current_fraction *= drawdown_multiplier
            current_fraction = _clamp(
                current_fraction, 0.0, self.params["max_risk_pct"]
            )
            self._last_size_label = "kelly"
            signal.reason += (
                f" · {regime} n={samples}, qL {lower_q:.1%}, "
                f"all-in {all_in_cost:.1%}, Kelly {current_fraction:.1%} bankroll"
            )

        # The engine accepts initial-capital fractions. Convert the intended
        # current-bankroll allocation so compounding and drawdown work live.
        return (self._equity * current_fraction) / max(self._initial_capital, 1e-9)

    def on_trade_settled(self, won: bool) -> None:
        self._record_outcome(won)

    def on_signal_resolved(self, won: bool, *, traded: bool) -> None:
        # A skipped no-edge candidate remains valuable calibration evidence in
        # a backtest. Executed trades were recorded by on_trade_settled.
        if not traded:
            self._record_outcome(won)

    def _record_outcome(self, won: bool) -> None:
        if self._pending_keys is None:
            return
        for key in self._pending_keys:
            wins, total = self._stats.setdefault(key, [0, 0])
            self._stats[key] = [wins + int(won), total + 1]
        self._pending_keys = None
        self._save_calibration()

    def size_label(self, risk_frac: float) -> str:
        return self._last_size_label
