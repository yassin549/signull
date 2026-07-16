"""Fast reusable backtest engine — strategy-agnostic simulation loop."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from strategies.base import CandleContext, Strategy, TickContext
from src.sizing import cap_stake_for_taker_fee, estimate_taker_fee

from .metrics import compute_metrics
from .types import BacktestResult, CandleDataset, TradeRecord

if TYPE_CHECKING:
    pass


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
    min_stake: float = 0.01,
    progress_callback: Callable[[dict], None] | None = None,
) -> BacktestResult:
    """
    Simulate *strategy* across resolved candles.

    Rules:
    - One entry per candle (first signal wins)
    - Stake = position_risk_fraction * initial_capital (capped at current equity)
    - Hold to candle close (binary settlement)
    """
    if hasattr(strategy, "prepare_backtest"):
        strategy.prepare_backtest(candles)  # type: ignore[attr-defined]

    t0 = time.perf_counter()
    capital = float(initial_capital)
    peak = capital
    max_drawdown = 0.0
    trades: list[TradeRecord] = []
    equity_curve: list[dict[str, float]] = [{"idx": 0, "equity": capital}]
    recent_outcomes: list[bool] = []
    wins_streak = 0
    losses_streak = 0
    equity_hist: list[float] = [capital]

    def report(candle_idx: int, trade: TradeRecord | None = None) -> None:
        if progress_callback is None:
            return
        payload = {
            "type": "progress", "phase": "backtesting",
            "candles_completed": candle_idx, "candles_total": len(candles),
            "equity": round(capital, 4),
            "equity_point": {"idx": candle_idx, "equity": round(capital, 4)},
        }
        if trade is not None:
            payload["trade"] = trade.__dict__
        progress_callback(payload)

    for candle_idx, candle in enumerate(candles, start=1):
        if capital < min_stake:
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

        entered = False
        signal = None
        entry_ts = 0
        entry_tick: TickContext | None = None

        for tick_t, up_p, down_p in candle.ticks:
            tick = TickContext(
                t=tick_t,
                up=up_p,
                down=down_p,
                seconds_into_candle=max(0.0, tick_t - candle.start_ts),
                seconds_to_close=max(0.0, candle.end_ts - tick_t),
            )
            signal = strategy.evaluate(tick, ctx, entered=entered)
            if signal is not None:
                entry_ts = tick_t
                entry_tick = tick
                entered = True
                break

        if signal is None or entry_tick is None:
            equity_curve.append({"idx": candle_idx, "equity": round(capital, 4)})
            report(candle_idx)
            continue

        risk_frac = strategy.position_risk_fraction(signal, entry_tick, ctx)
        entry_price = max(0.01, min(0.99, signal.price))
        fee_rate = max(0.0, float(signal.taker_fee_rate))
        desired_stake = min(initial_capital * risk_frac, capital)
        stake = cap_stake_for_taker_fee(
            desired_stake, entry_price, fee_rate, capital
        )
        if stake < min_stake:
            # A strategy may intentionally decline a marginal trade by
            # returning zero risk. That must skip this candle, not terminate
            # the whole backtest.
            strategy.on_signal_resolved(signal.side == candle.winner, traded=False)
            equity_curve.append({"idx": candle_idx, "equity": round(capital, 4)})
            report(candle_idx)
            continue

        size_label = ""
        if hasattr(strategy, "size_label"):
            size_label = strategy.size_label(risk_frac)  # type: ignore[attr-defined]

        shares = stake / entry_price
        won = signal.side == candle.winner
        entry_fee = estimate_taker_fee(stake, entry_price, fee_rate)
        pnl = _settle_trade(stake, entry_price, won, entry_fee)
        capital += pnl
        equity_hist.append(capital)

        peak = max(peak, capital)
        if peak > 0:
            dd = (peak - capital) / peak
            max_drawdown = max(max_drawdown, dd)

        reason = signal.reason
        if size_label:
            reason = f"{reason} · {size_label} ({risk_frac:.0%} of initial)"

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
                risk_pct=round(risk_frac * 100, 2),
                size_label=size_label,
                entry_fee=round(entry_fee, 4),
            )
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
    )
