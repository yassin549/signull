"""Quick sizing analysis for backtest strategies."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.backtest.data import fetch_candles
from src.backtest.engine import run_backtest
from src.backtest.registry import get_strategy


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    candles = fetch_candles("btc", count)
    for sid in ["smart_sizer", "prob_70", "tcn_sizer"]:
        r = run_backtest(get_strategy(sid), candles, initial_capital=100.0)
        wins = [t for t in r.trades if t.won]
        losses = [t for t in r.trades if not t.won]
        aw = sum(t.risk_pct for t in wins) / len(wins) if wins else 0.0
        al = sum(t.risk_pct for t in losses) / len(losses) if losses else 0.0
        sw = sum(t.stake for t in wins)
        sl = sum(t.stake for t in losses)
        pw = sum(t.pnl for t in wins)
        pl = sum(t.pnl for t in losses)
        big = sum(1 for t in r.trades if t.size_label == "big")
        med = sum(1 for t in r.trades if t.size_label == "medium")
        sm = sum(1 for t in r.trades if t.size_label == "small")
        print(f"=== {sid} ===")
        print(f"  end=${r.ending_capital:.2f} return={r.total_return_pct:+.1f}%")
        print(f"  avg risk%  wins={aw:.1f}  losses={al:.1f}")
        print(f"  total stake  wins=${sw:.1f}  losses=${sl:.1f}")
        print(f"  total pnl    wins=${pw:+.2f}  losses=${pl:+.2f}")
        print(f"  sizes: big={big} medium={med} small={sm}")
        print()


if __name__ == "__main__":
    main()