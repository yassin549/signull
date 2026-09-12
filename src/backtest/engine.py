"""Strategy-agnostic backtest engine that mirrors the live Predict.fun bot.

There is a single equity book, and it is the *realistic* one:

* Real trade prints (Predict.fun match tape) drive strategy evaluation.
* Binance 1-second spot (with the candle-open "price to beat") drives the
  same ±$ signal the live bot uses.
* A signal places a live-style limit buy at the signal price.  The order only
  becomes a trade if a later real print trades at or through the limit.
* Taker fees, the $0.01 tick rounding, the 10s-to-close guard, the minimum
  stake, and live stake sizing (fixed USD or % via ``src.sizing``) all apply.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from strategies.base import CandleContext, Strategy, TickContext, TradeSignal
from src.sizing import (
    cap_stake_for_taker_fee,
    compute_stake,
    estimate_taker_fee,
)

from .btc import BtcSeries
from .fills import simulate_live_limit_buy
from .metrics import compute_metrics
from .types import BacktestResult, CandleDataset, TradeRecord

LATE_CANDLE_SEC = 10.0
MIN_STAKE = 0.01


def _settle_trade(
    stake: float,
    entry_price: float,
    won: bool,
    entry_fee: float = 0.0,
) -> float:
    """Return PnL for a binary market buy held to resolution."""
    if entry_price <= 0 or stake <= 0:
        return 0.0
    if won:
        shares = stake / entry_price
        return shares * 1.0 - stake - entry_fee
    return -stake - entry_fee


def run_backtest(
    strategy: Strategy,
    candles: list[CandleDataset],
    *,
    initial_capital: float = 100.0,
    use_fixed_stake: bool = False,
    fixed_stake_usdc: float = 1.0,
    taker_fee_rate: float = 0.02,
    maker_fee_rate: float = 0.0,
    asset: str = "btc",
    btc_series: BtcSeries | None = None,
    load_btc: bool = True,
    invert_signals: bool = False,
    progress_callback: Callable[[dict], None] | None = None,
) -> BacktestResult:
    """Simulate *strategy* across resolved candles with live execution."""
    if hasattr(strategy, "prepare_backtest"):
        try:
            strategy.prepare_backtest(candles, progress_callback=progress_callback)  # type: ignore[attr-defined]
        except TypeError:
            strategy.prepare_backtest(candles)  # type: ignore[attr-defined]

    if btc_series is None and load_btc and candles and candles[0].start_ts > 1_500_000_000:
        btc_series = _load_btc_series(candles, progress_callback)

    t0 = time.perf_counter()
    capital = float(initial_capital)
    peak = capital
    max_drawdown = 0.0
    trades: list[TradeRecord] = []
    equity_curve: list[dict[str, Any]] = [{"idx": 0, "equity": capital}]
    recent_outcomes: list[bool] = []
    wins_streak = 0
    losses_streak = 0
    equity_hist: list[float] = [capital]
    fetch_failures = 0

    sim_store = getattr(strategy, "prob_store", None) or getattr(strategy, "sim_store", None)
    if sim_store is None:
        try:
            from src.ml.direction_probs import DirectionProbStore
            sim_store = DirectionProbStore.load_auto()
        except Exception:
            sim_store = None

    def report(candle_idx: int, trade: TradeRecord | None = None) -> None:
        if progress_callback is None:
            return
        payload: dict[str, Any] = {
            "type": "progress",
            "phase": "backtesting",
            "candles_completed": candle_idx,
            "candles_total": len(candles),
            "equity": round(capital, 4),
            "equity_point": {"idx": candle_idx, "equity": round(capital, 4)},
        }
        if trade is not None:
            payload["trade"] = trade.__dict__
        progress_callback(payload)

    for candle_idx, candle in enumerate(candles, start=1):
        if capital < MIN_STAKE:
            break

        wins_recent = sum(recent_outcomes)
        mom = capital - equity_hist[-5] if len(equity_hist) >= 5 else 0.0
        strategy.on_account_update(
            capital,
            initial_capital,
            peak,
            wins_recent=wins_recent,
            wins_streak=wins_streak,
            losses_streak=losses_streak,
            equity_momentum=mom,
        )

        ctx = CandleContext(
            slug=candle.slug,
            title=candle.title,
            start_ts=candle.start_ts,
            end_ts=candle.end_ts,
            winner=candle.winner,
        )
        beat = btc_series.open_at(candle.start_ts) if btc_series is not None else None

        entered = False
        signal: TradeSignal | None = None
        entry_ts = 0
        entry_tick: TickContext | None = None

        for tick_t, up_p, down_p in candle.ticks:
            tick = TickContext(
                t=tick_t,
                up=up_p,
                down=down_p,
                seconds_into_candle=max(0.0, tick_t - candle.start_ts),
                seconds_to_close=max(0.0, candle.end_ts - tick_t),
                btc_price=btc_series.price_at(tick_t) if btc_series is not None else None,
                btc_price_to_beat=beat,
            )
            signal = strategy.evaluate(tick, ctx, entered=entered)
            if signal is not None:
                if invert_signals:
                    original_side = signal.side
                    other = "down" if original_side == "up" else "up"
                    other_price = float(tick.down) if other == "down" else float(tick.up)
                    signal = TradeSignal(
                        side=other,
                        price=other_price,
                        reason=(
                            f"{signal.reason} · inverted {original_side.upper()}→{other.upper()}"
                        ),
                        taker_fee_rate=signal.taker_fee_rate,
                    )
                entry_ts = tick_t
                entry_tick = tick
                entered = True
                break

        if signal is None or entry_tick is None:
            equity_curve.append({"idx": candle_idx, "equity": round(capital, 4)})
            report(candle_idx)
            continue

        entry_price = round(max(0.01, min(0.99, float(signal.price))), 2)
        signal.price = entry_price
        fee_rate = float(taker_fee_rate)

        # --- live entry guards -------------------------------------------------
        fill = None
        reason_label = ""
        risk_frac = 0.0
        size_label = ""
        if entry_tick.seconds_to_close < LATE_CANDLE_SEC:
            reason_label = "late_candle"
        else:
            if use_fixed_stake:
                risk_frac = 0.0
                size_label = f"${fixed_stake_usdc:.2f} fixed"
                stake = compute_stake(
                    0.0, initial_capital, capital, fixed_stake=fixed_stake_usdc,
                )
            else:
                risk_frac = strategy.position_risk_fraction(signal, entry_tick, ctx)
                size_label = strategy.size_label(risk_frac) if hasattr(strategy, "size_label") else ""
                stake = compute_stake(risk_frac, initial_capital, capital)

            stake = cap_stake_for_taker_fee(stake, entry_price, fee_rate, capital)
            if stake < MIN_STAKE:
                reason_label = "stake_too_small"
            else:
                fill = simulate_live_limit_buy(
                    side=signal.side,
                    limit_price=entry_price,
                    entry_ts=entry_ts,
                    seconds_to_close=entry_tick.seconds_to_close,
                    ticks=candle.ticks,
                    end_ts=candle.end_ts,
                )
                reason_label = fill.reason

        filled = fill is not None and fill.filled
        if not filled:
            entry_fee = 0.0
            shares = 0.0
            pnl = 0.0
            won = False
            trade = TradeRecord(
                candle_slug=candle.slug,
                candle_title=candle.title,
                side=signal.side,
                entry_price=round(entry_price, 4),
                stake=0.0,
                shares=0.0,
                winner=candle.winner,
                won=False,
                pnl=0.0,
                equity_after=round(capital, 4),
                entry_ts=entry_ts,
                reason=(
                    f"{signal.reason} · limit @ {entry_price:.2f} unfilled ({reason_label})"
                ),
                risk_pct=0.0,
                size_label=size_label,
                entry_fee=0.0,
                filled=False,
                fill_reason=reason_label,
                limit_price=round(entry_price, 4),
                synthetic=candle.synthetic,
            )
            _attach_sim_probs(trade, candle, entry_ts, entry_tick, sim_store)
            trades.append(trade)
            strategy.on_signal_resolved(signal.side == candle.winner, traded=False)
            equity_curve.append({"idx": candle_idx, "equity": round(capital, 4), "unfilled": True})
            report(candle_idx, trade)
            continue

        entry_fee = estimate_taker_fee(stake, entry_price, fee_rate)
        shares = stake / entry_price
        won = signal.side == candle.winner
        pnl = _settle_trade(stake, entry_price, won, entry_fee)
        capital += pnl
        equity_hist.append(capital)

        peak = max(peak, capital)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - capital) / peak)

        if won:
            wins_streak += 1
            losses_streak = 0
        else:
            wins_streak = 0
            losses_streak += 1
        strategy.on_trade_settled(won)
        strategy.on_signal_resolved(won, traded=True)
        recent_outcomes.append(won)
        if len(recent_outcomes) > 10:
            recent_outcomes.pop(0)

        reason = f"{signal.reason} · limit @ {entry_price:.2f} filled ({reason_label})"
        if size_label:
            reason = f"{reason} · {size_label}"
        trade = TradeRecord(
            candle_slug=candle.slug,
            candle_title=candle.title,
            side=signal.side,
            entry_price=round(entry_price, 4),
            stake=round(stake, 4),
            shares=round(shares, 4),
            winner=candle.winner,
            won=won,
            pnl=round(pnl, 4),
            equity_after=round(capital, 4),
            entry_ts=entry_ts,
            reason=reason,
            risk_pct=round(risk_frac * 100, 2) if not use_fixed_stake else 0.0,
            size_label=size_label,
            entry_fee=round(entry_fee, 4),
            filled=True,
            fill_reason=reason_label,
            limit_price=round(entry_price, 4),
            synthetic=candle.synthetic,
        )
        _attach_sim_probs(trade, candle, entry_ts, entry_tick, sim_store)
        trades.append(trade)
        equity_curve.append({"idx": candle_idx, "equity": round(capital, 4)})
        report(candle_idx, trade)

    elapsed_ms = (time.perf_counter() - t0) * 1000
    return compute_metrics(
        strategy_id=strategy.meta.id,
        strategy_name=strategy.meta.name,
        params=dict(strategy.params),
        initial_capital=initial_capital,
        ending_capital=capital,
        candles_loaded=len(candles),
        trades=trades,
        equity_curve=equity_curve,
        max_drawdown_pct=max_drawdown * 100,
        elapsed_ms=elapsed_ms,
        fetch_failures=fetch_failures,
        synthetic_candles=sum(1 for c in candles if c.synthetic),
        invert_signals=invert_signals,
    )


def _load_btc_series(
    candles: list[CandleDataset],
    progress_callback: Callable[[dict], None] | None,
) -> BtcSeries | None:
    if not candles:
        return None
    return BtcSeries.load(candles[0].start_ts, candles[-1].end_ts, progress_callback=progress_callback)


def _attach_sim_probs(
    trade: TradeRecord,
    candle: CandleDataset,
    entry_ts: int,
    entry_tick: TickContext,
    sim_store,
) -> None:
    sim_entry_prob: float | None = None
    sim_lifetime_probs: list[dict[str, Any]] = []
    has_tcn = sim_store is not None and sim_store.has_candle(candle.start_ts)
    probs_series = sim_store.prob_up_series(candle.start_ts) if has_tcn else None
    if probs_series is not None:
        s_entry = max(0, min(299, int(entry_ts - candle.start_ts)))
        sim_entry_prob = round(float(probs_series[s_entry]), 4)
        sim_lifetime_probs = [
            {"t": candle.start_ts + s, "s": s, "prob": round(float(probs_series[s]), 4)}
            for s in range(s_entry, 300)
        ]
    else:
        s_entry = max(0, int(entry_ts - candle.start_ts))
        sim_entry_prob = round(float(entry_tick.up), 4)
        for tick_t, up_p, _down_p in candle.ticks:
            if tick_t >= entry_ts:
                sim_lifetime_probs.append({
                    "t": tick_t,
                    "s": max(0, int(tick_t - candle.start_ts)),
                    "prob": round(float(up_p), 4),
                })
        if not sim_lifetime_probs:
            sim_lifetime_probs = [{"t": entry_ts, "s": s_entry, "prob": sim_entry_prob}]
    trade.sim_entry_prob = sim_entry_prob
    trade.sim_lifetime_probs = sim_lifetime_probs
