"""Inspect Signull 1.0's observed-price threshold-crossing entries."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.backtest.data import fetch_candles
from src.backtest.engine import run_backtest, _settle_trade
from src.backtest.registry import get_strategy


def main() -> None:
    candles = fetch_candles("btc", 80, use_cache=True)

    # Pure math check
    for p in (0.70, 0.80):
        pnl = _settle_trade(50.0, p, True)
        print(f"win $50 @{p:.2f} => +${pnl:.2f}  (formula stake*(1-p)/p)")

    for thr in (0.70, 0.80):
        r = run_backtest(
            get_strategy("signull_1_0", {"threshold": thr}),
            candles,
            initial_capital=100.0,
        )
        prices = {t.entry_price for t in r.trades}
        big_wins = [t for t in r.trades if t.won and abs(t.stake - 50) < 0.01]
        sample = big_wins[0] if big_wins else (r.trades[0] if r.trades else None)
        print(
            f"threshold={thr:.2f} trades={r.candles_traded} "
            f"observed_entry_prices={sorted(prices)} "
            f"end=${r.ending_capital:.2f}"
        )
        if sample:
            print(
                f"  sample stake=${sample.stake:.2f} entry={sample.entry_price:.2f} "
                f"won={sample.won} pnl={sample.pnl:+.2f}"
            )
        assert all(p >= thr for p in prices), prices


if __name__ == "__main__":
    main()
