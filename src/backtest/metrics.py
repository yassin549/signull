"""Performance metrics for backtest results."""

from __future__ import annotations

from .types import BacktestResult, TradeRecord


def compute_metrics(
    *,
    strategy_id: str,
    strategy_name: str,
    params: dict,
    initial_capital: float,
    ending_capital: float,
    candles_loaded: int,
    trades: list[TradeRecord],
    equity_curve: list[dict[str, float]],
    max_drawdown_pct: float,
    elapsed_ms: float,
) -> BacktestResult:
    wins = [t for t in trades if t.won]
    losses = [t for t in trades if not t.won]

    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))

    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    traded = len(trades)
    win_rate = (len(wins) / traded * 100) if traded else 0.0
    total_return = ((ending_capital - initial_capital) / initial_capital * 100) if initial_capital else 0.0

    return BacktestResult(
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        params=params,
        initial_capital=round(initial_capital, 2),
        ending_capital=round(ending_capital, 2),
        total_return_pct=round(total_return, 2),
        candles_loaded=candles_loaded,
        candles_traded=traded,
        wins=len(wins),
        losses=len(losses),
        win_rate=round(win_rate, 2),
        max_drawdown_pct=round(max_drawdown_pct, 2),
        profit_factor=round(profit_factor, 2) if profit_factor != float("inf") else 999.0,
        avg_win=round(gross_win / len(wins), 2) if wins else 0.0,
        avg_loss=round(-gross_loss / len(losses), 2) if losses else 0.0,
        trades=trades,
        equity_curve=equity_curve,
        elapsed_ms=round(elapsed_ms, 1),
    )