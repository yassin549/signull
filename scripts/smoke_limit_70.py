"""Smoke-test Signull 1.0 vs peer strategies."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.backtest.data import fetch_candles
from src.backtest.engine import run_backtest
from src.backtest.registry import get_strategy, list_strategies


def main() -> None:
    print("strategies:", [(s["id"], s["name"]) for s in list_strategies()])
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    candles = fetch_candles("btc", count, use_cache=True)

    for sid in ["signull_1_0", "smart_sizer", "prob_70"]:
        r = run_backtest(get_strategy(sid), candles, initial_capital=100.0).to_dict()
        print(
            f"{r['strategy_name']:14s} end=${r['ending_capital']:7.2f} "
            f"ret={r['total_return_pct']:+6.1f}% "
            f"trades={r['candles_traded']} "
            f"WR={r['win_rate']:.1f}% "
            f"DD=-{r['max_drawdown_pct']:.1f}%"
        )
        if sid != "signull_1_0":
            continue
        big = sum(1 for t in r["trades"] if t.get("size_label") == "big")
        small = sum(1 for t in r["trades"] if t.get("size_label") == "small")
        print(f"               sizes: big={big} small={small}")


if __name__ == "__main__":
    main()
