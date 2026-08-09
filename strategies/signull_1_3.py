"""Signull 1.3 — speed + calm-volatility sizing with retrace exits.

Entry thesis
------------
Buy the first side observed at or above the favourite threshold (default 70¢).
Entry is independent from tick volatility — volatility never blocks trade entry.

Sizing thesis (absolute dollars, not bankroll %)
------------------------------------------------
- Faster threshold hit → larger stake
- Calmer pre-entry odds path (few/no 0.50 probability lead switches) → larger stake ($50)
- Higher 0.50 probability regime volatility (frequent/sharp switches >0.5 ↔ <0.5) → smaller stake ($1)
- Stake is clamped to [$min_stake_usdc, $max_stake_usdc] (default $1–$50)

Exit / re-entry
---------------
If our side retraces to the exit level (default 50¢), close the position and
wait for whichever side next reaches the entry threshold again. Multiple
entries per candle are allowed after an early exit.
"""

from __future__ import annotations

import math

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal

STRATEGY_CLASS = "Signull13Strategy"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class Signull13Strategy(Strategy):
    """Enter at 70¢; size by speed + 0.5-flip volatility; exit on 50¢ retrace; re-enter."""

    meta = StrategyMeta(
        id="signull_1_3",
        name="Signull 1.3 (Speed + Calm Vol)",
        description=(
            "Buys the first side at or above 70¢ independent of tick volatility. "
            "Stake scales from $1 to $50: faster hits and calmer pre-entry odds (fewer 0.50 "
            "probability lead switches) get larger size; higher 0.50 flip volatility "
            "yields smaller bets. If the position retraces to 50¢, exit and wait for the "
            "next 70¢ cross (any side)."
        ),
        default_params={
            "threshold": 0.70,
            "exit_retrace_price": 0.50,
            # Absolute stake bounds (Polymarket min order ~$1).
            "min_stake_usdc": 1.0,
            "max_stake_usdc": 50.0,
            "fast_hit_seconds": 60.0,
            "slow_hit_seconds": 240.0,
            # Pre-entry probability switches across 0.50 (>0.5 vs <0.5).
            # At/below low_volatility (0 flips) → max size contribution.
            # At/above high_volatility (>=2 flips) → min size contribution (small bet).
            "low_volatility": 0.0,
            "high_volatility": 2.0,
            "volatility_window": 6,
            "drawdown_stop_pct": 0.15,
            "min_drawdown_multiplier": 0.25,
            "taker_fee_rate": 0.07,
            # Blend: speed matters a bit more than calmness.
            "time_weight": 0.60,
            "vol_weight": 0.40,
        },
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._normalise_params()
        self._active_slug: str | None = None
        self._ticks: list[TickContext] = []
        self._last_size_label = "flat"
        self._last_features: dict[str, float] = {}
        self._last_stake_usdc = 0.0

    def _normalise_params(self) -> None:
        p = self.params
        defaults = self.meta.default_params
        for key in (
            "threshold",
            "exit_retrace_price",
            "min_stake_usdc",
            "max_stake_usdc",
            "fast_hit_seconds",
            "slow_hit_seconds",
            "low_volatility",
            "high_volatility",
            "drawdown_stop_pct",
            "min_drawdown_multiplier",
            "taker_fee_rate",
            "time_weight",
            "vol_weight",
        ):
            try:
                value = float(p.get(key, defaults[key]))
            except (TypeError, ValueError):
                value = float(defaults[key])
            # NaN guard
            p[key] = float(defaults[key]) if value != value else value

        p["threshold"] = _clamp(p["threshold"], 0.50, 0.99)
        p["exit_retrace_price"] = _clamp(
            p["exit_retrace_price"], 0.01, p["threshold"] - 0.01
        )
        p["min_stake_usdc"] = max(1.0, p["min_stake_usdc"])
        p["max_stake_usdc"] = max(p["min_stake_usdc"], p["max_stake_usdc"])
        p["fast_hit_seconds"] = max(0.0, p["fast_hit_seconds"])
        p["slow_hit_seconds"] = max(
            p["fast_hit_seconds"] + 1e-9, p["slow_hit_seconds"]
        )
        p["low_volatility"] = max(0.0, p["low_volatility"])
        p["high_volatility"] = max(
            p["low_volatility"] + 1e-9, p["high_volatility"]
        )
        p["drawdown_stop_pct"] = _clamp(p["drawdown_stop_pct"], 0.0, 1.0)
        p["min_drawdown_multiplier"] = _clamp(
            p["min_drawdown_multiplier"], 0.0, 1.0
        )
        p["taker_fee_rate"] = _clamp(p["taker_fee_rate"], 0.0, 1.0)
        tw = max(0.0, p["time_weight"])
        vw = max(0.0, p["vol_weight"])
        total = tw + vw
        if total <= 0:
            tw, vw, total = 0.60, 0.40, 1.0
        p["time_weight"] = tw / total
        p["vol_weight"] = vw / total
        try:
            p["volatility_window"] = max(
                2, int(float(p.get("volatility_window", 6)))
            )
        except (TypeError, ValueError):
            p["volatility_window"] = int(defaults["volatility_window"])

    def _reset_candle_if_needed(self, candle: CandleContext) -> None:
        if self._active_slug != candle.slug:
            self._active_slug = candle.slug
            self._ticks = []

    def _remember_tick(self, tick: TickContext, candle: CandleContext) -> None:
        self._reset_candle_if_needed(candle)
        self._ticks.append(tick)
        window = int(self.params["volatility_window"])
        if len(self._ticks) > max(window, 32):
            # Keep a slightly longer path than the vol window so re-entries
            # after an exit still see recent motion.
            self._ticks = self._ticks[-max(window, 32) :]

    def _pre_entry_volatility(self) -> float:
        """Measure switches across 0.50 probability (>0.5 to <0.5 or vice versa)."""
        window = int(self.params["volatility_window"])
        recent = self._ticks[-window:]
        if len(recent) < 2:
            return 0.0
        switches = 0.0
        for prev, cur in zip(recent, recent[1:]):
            prev_above = prev.up > 0.50
            cur_above = cur.up > 0.50
            if prev_above != cur_above:
                switches += 1.0 + abs(cur.up - prev.up)
        return switches

    def _time_score(self, seconds_into_candle: float) -> float:
        """1.0 = hit as fast as (or faster than) fast_hit_seconds; 0 = slow."""
        fast = self.params["fast_hit_seconds"]
        slow = self.params["slow_hit_seconds"]
        return 1.0 - _clamp((seconds_into_candle - fast) / (slow - fast), 0.0, 1.0)

    def _volatility_score(self, volatility: float) -> float:
        """1.0 = calm (low/0 0.5-flips → big bet); 0.0 = high volatility (frequent/sharp 0.5 flips → small bet)."""
        low = self.params["low_volatility"]
        high = self.params["high_volatility"]
        return 1.0 - _clamp((volatility - low) / (high - low), 0.0, 1.0)

    def _side_price(self, tick: TickContext, side: str) -> float:
        return tick.up if side == "up" else tick.down

    def evaluate(
        self, tick: TickContext, candle: CandleContext, *, entered: bool
    ) -> TradeSignal | None:
        # Always track path so volatility / re-entry see continuous history.
        self._remember_tick(tick, candle)
        if entered:
            return None

        threshold = self.params["threshold"]
        up_hit = tick.up >= threshold
        down_hit = tick.down >= threshold
        if not up_hit and not down_hit:
            return None

        volatility = self._pre_entry_volatility()
        # Volatility does NOT prevent entry; it only scales position size.

        if up_hit and down_hit:
            side = "up" if tick.up >= tick.down else "down"
        elif up_hit:
            side = "up"
        else:
            side = "down"
        price = self._side_price(tick, side)

        time_score = self._time_score(tick.seconds_into_candle)
        vol_score = self._volatility_score(volatility)
        self._last_features = {
            "seconds_to_70": float(tick.seconds_into_candle),
            "time_score": time_score,
            "volatility": volatility,
            "volatility_score": vol_score,
            "skipped_high_vol": 0.0,
        }

        return TradeSignal(
            side=side,
            price=price,
            reason=(
                f"{side.upper()} reached {threshold:.0%} in "
                f"{tick.seconds_into_candle:.0f}s; pre-entry 0.5-flip vol "
                f"{volatility:.2f} (low vol→big, high vol→small)"
            ),
            taker_fee_rate=self.params["taker_fee_rate"],
        )

    def should_exit(
        self,
        tick: TickContext,
        candle: CandleContext,
        *,
        side: str,
        entry_price: float,
    ) -> TradeSignal | None:
        """Close when our side retraces to the exit level (default 50¢)."""
        del entry_price
        self._remember_tick(tick, candle)
        exit_level = float(self.params["exit_retrace_price"])
        px = self._side_price(tick, side)
        if px > exit_level:
            return None
        return TradeSignal(
            side=side,
            price=px,
            reason=(
                f"{side.upper()} retraced to {px:.0%} ≤ {exit_level:.0%} — "
                f"exit and wait for next {self.params['threshold']:.0%} cross"
            ),
            taker_fee_rate=self.params["taker_fee_rate"],
        )

    def position_stake_usdc(
        self, signal: TradeSignal, tick: TickContext, candle: CandleContext
    ) -> float:
        """Absolute USDC stake in [$min, $max], scaled by speed + calmness."""
        del signal, tick, candle
        min_stake = float(self.params["min_stake_usdc"])
        max_stake = float(self.params["max_stake_usdc"])
        time_score = self._last_features.get("time_score", 0.0)
        vol_score = self._last_features.get("volatility_score", 0.0)
        tw = float(self.params["time_weight"])
        vw = float(self.params["vol_weight"])
        quality = _clamp(tw * time_score + vw * vol_score, 0.0, 1.0)

        stake = min_stake + (max_stake - min_stake) * quality

        peak = max(self._peak_equity, 1e-9)
        drawdown = max(0.0, (peak - self._equity) / peak)
        stop = max(self.params["drawdown_stop_pct"], 1e-9)
        floor = self.params["min_drawdown_multiplier"]
        drawdown_multiplier = max(floor, 1.0 - drawdown / stop)
        stake *= drawdown_multiplier

        # After drawdown haircut, still respect the exchange $1 minimum when
        # we intend to trade; if equity cannot cover $1 the engine will skip.
        stake = _clamp(stake, min_stake * drawdown_multiplier, max_stake)
        if stake < min_stake and self._equity >= min_stake:
            # Mild DD: keep at least the $1 floor so we still participate.
            stake = min_stake
        stake = min(stake, max_stake, max(0.0, self._equity))

        if quality >= 0.75:
            self._last_size_label = "fast-calm"
        elif quality >= 0.45:
            self._last_size_label = "mixed"
        else:
            self._last_size_label = "slow-noisy"

        self._last_stake_usdc = float(stake)
        return float(stake)

    def position_risk_fraction(
        self, signal: TradeSignal, tick: TickContext, candle: CandleContext
    ) -> float:
        """Engine still sizes as risk_frac * initial; map absolute stake → frac."""
        stake = self.position_stake_usdc(signal, tick, candle)
        if stake <= 0:
            return 0.0
        return stake / max(self._initial_capital, 1e-9)

    def size_label(self, risk_frac: float) -> str:
        del risk_frac
        if self._last_stake_usdc > 0:
            return f"{self._last_size_label} ${self._last_stake_usdc:.2f}"
        return self._last_size_label
