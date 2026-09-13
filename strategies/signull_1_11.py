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
            "Sends ONLY the last 5 BTC 5-minute candles (OHLCV) to an OpenRouter "
            "LLM at candle open and buys the side the model picks (always Up or "
            "Down). Pure price-action reasoning — no SIM model, odds or account "
            "input. Stake is scaled by confidence between $1 and $2. Live only."
        ),
        default_params={
            "min_stake_usd": 1.0,
            "max_stake_usd": 2.0,
            "taker_fee_rate": 0.02,
            "decision_delay_sec": 3.0,
            "entry_window_sec": 120.0,
            "btc_lookback_minutes": 30,
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
        self._stake_by_slug: dict[str, float] = {}
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

    def _actual_winner(
        self, slug: str, ticks: list[tuple[int, float, float]]
    ) -> tuple[str | None, dict | None]:
        """Resolve the candle's true direction from its OHLCV (close vs open)."""
        start = None
        try:
            start = int(str(slug).rsplit("-", 1)[-1])
        except (ValueError, IndexError):
            start = None
        if start is not None:
            try:
                rows = fetch_klines(start, start + 300, asset=self._asset)
                for bar in _group_5m(rows):
                    if int(bar["start"]) == start:
                        return ("up" if bar["close"] >= bar["open"] else "down"), bar
            except Exception as exc:
                logger.debug("actual winner fetch failed: %s", exc)
        if ticks:
            winner = None
            try:
                winner = winner_from_ticks(ticks, at_close=True)
            except Exception:
                winner = None
            if winner is None:
                winner = "up" if float(ticks[-1][1]) >= 0.5 else "down"
            return winner, None
        return None, None

    def _theoretical_pnl(self, stake: float, entry_price: float, won: bool) -> float:
        from src.sizing import estimate_taker_fee

        if entry_price <= 0 or stake <= 0:
            return 0.0
        fee = estimate_taker_fee(stake, entry_price, float(self.params.get("taker_fee_rate", 0.02)))
        if won:
            return stake / entry_price - stake - fee
        return -stake - fee

    def register_closed_candle(self, slug: str, ticks: list[tuple[int, float, float]]) -> bool:
        winner, _bar = self._actual_winner(slug, ticks)
        with self._state_lock:
            entry = next((e for e in reversed(self._ai_history) if e.get("slug") == slug), None)
            stake = self._stake_by_slug.get(slug)
            entry_price = entry.get("entry_price") if entry else None
            won: bool | None = None
            pnl: float | None = None
            if entry is not None and winner and stake and entry_price:
                won = entry.get("side") == winner
                pnl = self._theoretical_pnl(float(stake), float(entry_price), won)
                entry["won"] = bool(won)
                entry["winner"] = winner
                entry["stake"] = round(float(stake), 2)
                entry["pnl"] = round(float(pnl), 4)
            self._outcomes[slug] = {
                "slug": slug,
                "winner": winner,
                "won": won,
                "pnl": round(float(pnl), 4) if pnl is not None else None,
                "stake": round(float(stake), 2) if stake else None,
            }
            if len(self._outcomes) > 40:
                for old in sorted(self._outcomes)[:-30]:
                    self._outcomes.pop(old, None)
                    self._reported_outcomes.discard(old)
            self._ai_version += 1
        return False

    def on_trade_settled(self, won: bool, info: dict | None = None) -> None:
        with self._state_lock:
            slug = (info or {}).get("slug")
            entry = None
            if slug:
                entry = next((e for e in reversed(self._ai_history) if e.get("slug") == slug), None)
            if entry is None and self._ai_history:
                entry = self._ai_history[-1]
            if entry is not None:
                chosen = entry.get("side")
                entry["won"] = bool(won)
                if not entry.get("winner"):
                    entry["winner"] = chosen if won else ("down" if chosen == "up" else "up")
                if info:
                    if info.get("winner"):
                        entry["winner"] = info["winner"]
                    if info.get("stake") is not None:
                        entry["stake"] = round(float(info["stake"]), 2)
                    if info.get("pnl") is not None:
                        entry["pnl"] = round(float(info["pnl"]), 4)
                outcome = self._outcomes.get(entry.get("slug"))
                if outcome is not None:
                    outcome["won"] = bool(won)
                    if entry.get("winner"):
                        outcome["winner"] = entry["winner"]
                    if entry.get("pnl") is not None:
                        outcome["pnl"] = entry["pnl"]
            self._ai_version += 1

    # ── decision plumbing ───────────────────────────────────────────────────
    def _decide(self, tick: TickContext, candle: CandleContext):
        with self._state_lock:
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
        if outcome is not None:
            parts.append(self._outcome_text(outcome))
        parts.append(self._ohlcv_block(tick, candle))
        context_text = "\n\n".join(parts)

        decision = self._engine.decide(
            context_text,
            temperature=float(self.params.get("temperature", 0.2)),
            max_tokens=int(self.params.get("max_tokens", 700)),
        )

        side = decision.side
        entry_price = float(tick.up) if side == "up" else float(tick.down)
        if not (0.0 < entry_price < 1.0):
            entry_price = 0.5

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
                "entry_price": round(entry_price, 4),
                "stake": None,
                "winner": None,
                "won": None,
                "pnl": None,
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
        lines = ["PREVIOUS CANDLE RESULT:"]
        lines.append(f"- Actual: {str(winner).upper() if winner else 'unknown'}")
        if side:
            lines.append(f"- You predicted: {str(side).upper()}")
        if won is not None:
            lines.append(f"- Result: {'WIN' if won else 'LOSS'}")
        return "\n".join(lines)

    def _fetch_bars(self, now_ts: float) -> list[dict[str, float]]:
        lookback = max(10, int(self.params.get("btc_lookback_minutes", 30))) * 60
        try:
            rows = fetch_klines(int(now_ts) - lookback, int(now_ts) + 60, asset=self._asset)
        except Exception as exc:
            logger.debug("AI kline fetch failed: %s", exc)
            return []
        return _group_5m(rows)

    def _ohlcv_block(self, tick: TickContext, candle: CandleContext) -> str:
        """Only the last five completed 5-minute candles — nothing else."""
        bars = self._fetch_bars(tick.t)
        completed = [b for b in bars if int(b["start"]) != int(candle.start_ts)]
        last5 = completed[-5:]
        lines = ["LAST 5 BTC 5-MINUTE CANDLES (OHLCV, oldest -> newest):"]
        if last5:
            for bar in last5:
                lines.append(
                    f"- {_utc(bar['start'])} O {bar['open']:.2f} H {bar['high']:.2f} "
                    f"L {bar['low']:.2f} C {bar['close']:.2f} V {bar['vol']:.2f}"
                )
        else:
            lines.append("- (candle data unavailable)")
        lines.append(
            "Predict whether the NEXT 5-minute candle closes UP (above its open) or DOWN (below its open)."
        )
        return "\n".join(lines)

    # ── sizing ──────────────────────────────────────────────────────────────
    def _stake_for_confidence(self) -> float:
        confidence = self._last_confidence if self._last_confidence is not None else 0.5
        min_stake = float(self.params.get("min_stake_usd", 1.0))
        max_stake = float(self.params.get("max_stake_usd", 2.0))
        if max_stake < min_stake:
            min_stake, max_stake = max_stake, min_stake
        scaled = max(0.0, min(1.0, (confidence - 0.5) / 0.5))
        return min_stake + (max_stake - min_stake) * scaled

    def position_risk_fraction(
        self, signal: TradeSignal, tick: TickContext, candle: CandleContext
    ) -> float:
        del signal, tick, candle
        stake = self._stake_for_confidence()
        base = self._initial_capital or 100.0
        return stake / base if base else 0.0

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
        """Fixed $1-$2 stake scaled by confidence (works with USE_FIXED_STAKE)."""
        del signal, tick, wallet_balance
        stake = self._stake_for_confidence()
        confidence = self._last_confidence if self._last_confidence is not None else 0.5
        slug = getattr(candle, "slug", None)
        if slug:
            with self._state_lock:
                self._stake_by_slug[slug] = stake
                if len(self._stake_by_slug) > 100:
                    for old in sorted(self._stake_by_slug)[:-60]:
                        self._stake_by_slug.pop(old, None)
        base = float(initial or equity or 0.0)
        risk_frac = (stake / base) if base else 0.0
        return stake, f"AI {int(round(confidence * 100))}% conf · ${stake:.2f}", round(risk_frac, 4)

    def size_label(self, risk_frac: float) -> str:
        del risk_frac
        confidence = self._last_confidence if self._last_confidence is not None else 0.5
        return f"AI {int(round(confidence * 100))}% conf"

    # ── dashboard monitor ───────────────────────────────────────────────────
    def reset_ai(self) -> None:
        self._engine.reset()
        with self._state_lock:
            self._ai_history = []
            self._outcomes = {}
            self._reported_outcomes = set()
            self._stake_by_slug = {}
            self._decided_slug = None
            self._last_confidence = None
            self._last_context = ""
            self._ai_version += 1

    def ai_snapshot(self) -> dict:
        engine = self._engine.snapshot()
        with self._state_lock:
            history = list(self._ai_history[-30:])
            outcomes = list(self._outcomes.values())[-5:]
            settled = [e for e in self._ai_history if e.get("pnl") is not None]
            pnl_total = sum(float(e["pnl"]) for e in settled)
            wins = sum(1 for e in self._ai_history if e.get("won") is True)
            losses = sum(1 for e in self._ai_history if e.get("won") is False)
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
            "pnl_total": round(pnl_total, 4),
            "trades": len(settled),
            "wins": wins,
            "losses": losses,
            "version": self._ai_version,
        }
