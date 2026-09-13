"""Signull 1.11 — OpenRouter AI model that calls every candle at the open.

This strategy is deliberately *live only* (see ``meta.live_only``): it makes a
paid network call on every 5-minute candle and is not intended for backtests.

Cost model:
- The first candle sends the full market briefing to the model.
- Each later candle appends only a compact "previous candle result + new candle
  state" update, so the conversation (and prompt size) grows without resending
  the whole chart history.  ``AIDecisionEngine`` trims what is sent to the API.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal

from src.ai_engine import AIDecisionEngine
from src.markets import winner_from_ticks
from src.ml.btc_features import fetch_klines

logger = logging.getLogger(__name__)

STRATEGY_CLASS = "Signull11Strategy"


def _utc(ts: int | float) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        return "—"


def _group_5m(rows: list) -> list[dict[str, float]]:
    """Aggregate 1m Binance klines into 5m bars."""
    buckets: dict[int, dict[str, float]] = {}
    for row in rows:
        try:
            open_time_ms, o, h, low, close, vol = row[0], row[1], row[2], row[3], row[4], row[5]
        except (IndexError, TypeError):
            continue
        start = (int(open_time_ms) // 1000) // 300 * 300
        bar = buckets.get(start)
        if bar is None:
            buckets[start] = {
                "start": float(start), "open": float(o), "high": float(h),
                "low": float(low), "close": float(close), "vol": float(vol),
            }
        else:
            bar["high"] = max(bar["high"], float(h))
            bar["low"] = min(bar["low"], float(low))
            bar["close"] = float(close)
            bar["vol"] += float(vol)
    return [buckets[key] for key in sorted(buckets)]


class Signull11Strategy(Strategy):
    meta = StrategyMeta(
        id="signull_1_11",
        name="Signull 1.11 (AI OpenRouter)",
        description=(
            "Sends the BTC 5-minute chart context to an OpenRouter LLM at candle "
            "open and buys the side the model picks (always Up or Down). Stake is "
            "scaled by the model's confidence. Live only — never backtested."
        ),
        default_params={
            "min_risk_pct": 0.02,
            "max_risk_pct": 0.10,
            "decision_delay_sec": 3.0,
            "entry_window_sec": 120.0,
            "btc_lookback_minutes": 90,
            "temperature": 0.2,
            "max_tokens": 700,
            "asset": "btc",
        },
        live_only=True,
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._asset = str(self.params.get("asset", "btc")).lower()
        self._engine = AIDecisionEngine()
        self._state_lock = threading.Lock()
        self._decided_slug: str | None = None
        self._outcomes: dict[str, dict] = {}
        self._reported_outcomes: set[str] = set()
        self._ai_history: list[dict] = []
        self._last_confidence: float | None = None
        self._last_context: str = ""
        self._ai_version = 0

    # ── hooks ───────────────────────────────────────────────────────────────
    def _bump(self) -> None:
        self._ai_version += 1

    def evaluate(
        self, tick: TickContext, candle: CandleContext, *, entered: bool
    ) -> TradeSignal | None:
        if entered or candle.slug == self._decided_slug:
            return None

        delay = max(0.0, float(self.params.get("decision_delay_sec", 3.0)))
        if tick.seconds_into_candle < delay:
            return None
        # Only commit near the open. If the model is switched on mid-candle, wait
        # for the next candle instead of entering with half the move gone.
        entry_window = max(delay, float(self.params.get("entry_window_sec", 120.0)))
        if tick.seconds_into_candle > entry_window:
            self._decided_slug = candle.slug
            return None
        if tick.seconds_to_close < 10.0:
            return None

        self._decided_slug = candle.slug
        try:
            decision = self._decide(tick, candle)
        except Exception as exc:
            logger.warning("AI decision failed for %s: %s", candle.slug, exc)
            self._bump()
            return None

        side = decision.side
        price = float(tick.up) if side == "up" else float(tick.down)
        if not (0.0 < price < 1.0):
            price = 0.5
        reason = (
            f"AI {side.upper()} ({decision.confidence:.0%} conf, {decision.model}) — "
            f"{decision.reasoning[:180]}"
        )
        return TradeSignal(side=side, price=price, reason=reason)

    def register_closed_candle(self, slug: str, ticks: list[tuple[int, float, float]]) -> bool:
        winner: str | None = None
        up_open = up_close = None
        if ticks:
            try:
                winner = winner_from_ticks(ticks, at_close=True)
            except Exception:
                winner = None
            up_open = float(ticks[0][1])
            up_close = float(ticks[-1][1])
            if winner is None:
                winner = "up" if up_close >= 0.5 else "down"
        with self._state_lock:
            self._outcomes[slug] = {
                "slug": slug,
                "winner": winner,
                "up_open": up_open,
                "up_close": up_close,
                "ticks": len(ticks),
            }
            if len(self._outcomes) > 40:
                for old in sorted(self._outcomes)[:-30]:
                    self._outcomes.pop(old, None)
                    self._reported_outcomes.discard(old)
            self._ai_version += 1
        return False

    def on_trade_settled(self, won: bool) -> None:
        with self._state_lock:
            if self._ai_history:
                entry = self._ai_history[-1]
                chosen = entry.get("side")
                entry["won"] = bool(won)
                if not entry.get("winner") and chosen:
                    entry["winner"] = chosen if won else ("down" if chosen == "up" else "up")
                outcome = self._outcomes.get(entry.get("slug"))
                if outcome is not None:
                    outcome["won"] = bool(won)
                    if not outcome.get("winner") and chosen:
                        outcome["winner"] = chosen if won else ("down" if chosen == "up" else "up")
            self._ai_version += 1

    # ── decision plumbing ───────────────────────────────────────────────────
    def _decide(self, tick: TickContext, candle: CandleContext):
        with self._state_lock:
            is_first = not self._ai_history
            prev_slug = self._ai_history[-1]["slug"] if self._ai_history else None
            prev_side = self._ai_history[-1]["side"] if self._ai_history else None
            outcome = None
            if (
                prev_slug
                and prev_slug not in self._reported_outcomes
                and prev_slug in self._outcomes
            ):
                outcome = dict(self._outcomes[prev_slug])
                outcome["side"] = prev_side

        parts: list[str] = []
        if is_first:
            parts.append(
                "INITIAL BRIEFING: This is your first decision. Study the chart "
                "context below carefully; later candles will only send you the "
                "previous candle's result plus the fresh state."
            )
        elif outcome is not None:
            parts.append(self._outcome_text(outcome))
        else:
            parts.append("PREVIOUS CANDLE RESULT: awaiting settlement / no prior trade.")
        parts.append(self._market_block(tick, candle, detailed=is_first))
        context_text = "\n\n".join(parts)

        decision = self._engine.decide(
            context_text,
            temperature=float(self.params.get("temperature", 0.2)),
            max_tokens=int(self.params.get("max_tokens", 700)),
        )

        with self._state_lock:
            if outcome is not None and prev_slug:
                self._reported_outcomes.add(prev_slug)
            self._last_context = context_text
            self._last_confidence = decision.confidence
            self._ai_history.append({
                "slug": candle.slug,
                "ts": decision.ts,
                "time": _utc(decision.ts),
                "side": decision.side,
                "confidence": round(decision.confidence, 4),
                "reasoning": decision.reasoning,
                "key_factors": list(decision.key_factors),
                "model": decision.model,
                "latency_ms": round(decision.latency_ms, 1),
                "winner": None,
                "won": None,
            })
            if len(self._ai_history) > 200:
                self._ai_history = self._ai_history[-200:]
            self._ai_version += 1
        return decision

    @staticmethod
    def _outcome_text(pending: dict) -> str:
        side = pending.get("side")
        winner = pending.get("winner")
        won = pending.get("won")
        lines = [f"PREVIOUS CANDLE ({pending.get('slug')}) RESULT:"]
        lines.append(f"- Actual outcome: {str(winner).upper() if winner else 'unknown'}")
        if side:
            lines.append(f"- You predicted: {str(side).upper()}")
        if won is not None:
            lines.append(f"- Result: {'WIN' if won else 'LOSS'}")
        if pending.get("up_open") is not None and pending.get("up_close") is not None:
            lines.append(
                f"- Up token odds moved {pending['up_open']:.2f} -> {pending['up_close']:.2f}"
            )
        return "\n".join(lines)

    def _fetch_bars(self, now_ts: float) -> list[dict[str, float]]:
        lookback = max(10, int(self.params.get("btc_lookback_minutes", 90))) * 60
        try:
            rows = fetch_klines(int(now_ts) - lookback, int(now_ts) + 60, asset=self._asset)
        except Exception as exc:
            logger.debug("AI kline fetch failed: %s", exc)
            return []
        return _group_5m(rows)

    def _market_block(self, tick: TickContext, candle: CandleContext, *, detailed: bool = False) -> str:
        spot = tick.btc_price
        beat = tick.btc_price_to_beat
        up = float(tick.up)
        down = float(tick.down)
        implied = up / (up + down) if (up + down) > 0 else 0.5

        lines: list[str] = []
        lines.append(f"NOW: {_utc(tick.t)}")
        lines.append(
            f"CURRENT 5M CANDLE: {candle.slug} — started {_utc(candle.start_ts)}, "
            f"{tick.seconds_into_candle:.0f}s elapsed, {tick.seconds_to_close:.0f}s to close"
        )
        if spot is not None and beat is not None:
            delta = float(spot) - float(beat)
            pct = (delta / float(beat) * 100.0) if beat else 0.0
            lines.append(
                f"BTC PRICE: spot {float(spot):.2f}, candle open (beat) {float(beat):.2f}, "
                f"delta {'+' if delta >= 0 else ''}{delta:.2f} ({pct:+.3f}%)"
            )
        elif spot is not None:
            lines.append(f"BTC PRICE: spot {float(spot):.2f} (open beat unavailable)")
        lines.append(
            f"MARKET ODDS: Up {up:.3f} / Down {down:.3f} — implied P(Up) = {implied:.3f}"
        )
        if tick.sim_prob is not None:
            lines.append(f"SIM MODEL P(Up): {float(tick.sim_prob):.3f}")

        account = (
            f"ACCOUNT: equity {self._equity:.2f}, initial {self._initial_capital:.2f}, "
            f"return {((self._equity / self._initial_capital - 1) * 100) if self._initial_capital else 0:+.2f}%, "
            f"wins streak {self._wins_streak}, losses streak {self._losses_streak}, "
            f"recent wins {self._wins_recent}/10"
        )
        lines.append(account)

        bars = self._fetch_bars(tick.t)
        if bars:
            lines.append("RECENT 5M BARS (oldest -> newest):")
            for bar in bars[(-18 if detailed else -12):]:
                direction = "UP" if bar["close"] >= bar["open"] else "DOWN"
                change = (bar["close"] - bar["open"]) / bar["open"] * 100 if bar["open"] else 0.0
                lines.append(
                    f"- {_utc(bar['start'])} O {bar['open']:.2f} H {bar['high']:.2f} "
                    f"L {bar['low']:.2f} C {bar['close']:.2f} ({change:+.2f}%) {direction}"
                )
        else:
            lines.append("RECENT 5M BARS: unavailable this candle")

        if self._ai_history:
            lines.append("YOUR RECENT DECISIONS (newest last):")
            for entry in self._ai_history[-5:]:
                won = entry.get("won")
                result = "pending" if won is None else ("WIN" if won else "LOSS")
                lines.append(
                    f"- {entry['time']} {entry['side'].upper()} @ {entry['confidence']:.0%} "
                    f"-> {str(entry.get('winner') or '?').upper()} ({result})"
                )

        return "\n".join(lines)

    # ── sizing ──────────────────────────────────────────────────────────────
    def _risk_fraction(self) -> float:
        confidence = self._last_confidence if self._last_confidence is not None else 0.6
        min_pct = float(self.params.get("min_risk_pct", 0.02))
        max_pct = float(self.params.get("max_risk_pct", 0.10))
        if max_pct < min_pct:
            min_pct, max_pct = max_pct, min_pct
        scaled = max(0.0, min(1.0, (confidence - 0.5) / 0.5))
        return min_pct + (max_pct - min_pct) * scaled

    def position_risk_fraction(
        self, signal: TradeSignal, tick: TickContext, candle: CandleContext
    ) -> float:
        del signal, tick, candle
        return self._risk_fraction()

    def stake_override(
        self,
        signal: TradeSignal,
        tick: TickContext,
        candle: CandleContext,
        *,
        equity: float = 0.0,
        initial: float = 0.0,
        wallet_balance: float | None = None,
    ) -> tuple[float, str, float]:
        """Confidence-scaled stake that works even when USE_FIXED_STAKE=true."""
        del signal, tick, candle, wallet_balance
        risk_frac = self._risk_fraction()
        base = float(equity or initial or 0.0)
        stake = max(0.0, base * risk_frac)
        confidence = self._last_confidence if self._last_confidence is not None else 0.6
        return stake, f"AI {int(round(confidence * 100))}% conf", round(risk_frac, 4)

    def size_label(self, risk_frac: float) -> str:
        del risk_frac
        confidence = self._last_confidence if self._last_confidence is not None else 0.6
        return f"AI {int(round(confidence * 100))}% conf"

    # ── dashboard monitor ───────────────────────────────────────────────────
    def reset_ai(self) -> None:
        self._engine.reset()
        with self._state_lock:
            self._ai_history = []
            self._outcomes = {}
            self._reported_outcomes = set()
            self._decided_slug = None
            self._last_confidence = None
            self._last_context = ""
            self._ai_version += 1

    def ai_snapshot(self) -> dict:
        engine = self._engine.snapshot()
        with self._state_lock:
            history = list(self._ai_history[-30:])
            outcomes = list(self._outcomes.values())[-5:]
        return {
            "status": engine["status"],
            "last_error": engine["last_error"],
            "model": engine["model"],
            "api_key_set": engine["api_key_set"],
            "usage": engine["usage"],
            "messages": engine["messages"],
            "history": history,
            "last_context": self._last_context[-1500:],
            "pending_outcome": outcomes[-1] if outcomes else None,
            "version": self._ai_version,
        }
