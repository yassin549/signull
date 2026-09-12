"""Performance metrics for backtest results (single realistic book)."""

from __future__ import annotations

from .types import BacktestResult, TradeRecord


def _pnl_summary(trades: list[TradeRecord]) -> tuple[int, int, float, float, float, float]:
    """Return wins, losses, win_rate, profit_factor, avg_win, avg_loss."""
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
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (-gross_loss / len(losses)) if losses else 0.0
    return len(wins), len(losses), win_rate, profit_factor, avg_win, avg_loss


def _round_pf(profit_factor: float) -> float:
    return round(profit_factor, 2) if profit_factor != float("inf") else 999.0


def compute_metrics(
    *,
    strategy_id: str,
    strategy_name: str,
    params: dict,
    initial_capital: float,
    ending_capital: float,
    candles_loaded: int,
    trades: list[TradeRecord],
    equity_curve: list[dict],
    max_drawdown_pct: float,
    elapsed_ms: float,
    fetch_failures: int = 0,
    synthetic_candles: int = 0,
    invert_signals: bool = False,
) -> BacktestResult:
    filled = [t for t in trades if t.filled]
    unfilled = len(trades) - len(filled)
    fill_rate = (len(filled) / len(trades) * 100) if trades else 100.0
    wins, losses, win_rate, profit_factor, avg_win, avg_loss = _pnl_summary(filled)
    total_return = (
        ((ending_capital - initial_capital) / initial_capital * 100) if initial_capital else 0.0
    )

    return BacktestResult(
        strategy_id=strategy_id,
        strategy_name=strategy_name,
        params=params,
        initial_capital=round(initial_capital, 2),
        ending_capital=round(ending_capital, 2),
        total_return_pct=round(total_return, 2),
        candles_loaded=candles_loaded,
        candles_traded=len(filled),
        wins=wins,
        losses=losses,
        win_rate=round(win_rate, 2),
        max_drawdown_pct=round(max_drawdown_pct, 2),
        profit_factor=_round_pf(profit_factor),
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        trades=trades,
        equity_curve=equity_curve,
        elapsed_ms=round(elapsed_ms, 1),
        fills=len(filled),
        unfilled=unfilled,
        fill_rate=round(fill_rate, 2),
        fetch_failures=fetch_failures,
        synthetic_candles=synthetic_candles,
        invert_signals=invert_signals,
    )
