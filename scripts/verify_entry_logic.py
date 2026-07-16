"""Prove Signull 1.0 enters only on a threshold crossing."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from strategies.base import CandleContext, TickContext
from strategies.signull_1_0 import Signull10Strategy, first_limit_hit
from src.backtest.data import fetch_candles
from src.backtest.engine import run_backtest
from src.backtest.registry import get_strategy


def main() -> None:
    # Unit: 50/50 mid must NOT fill a 10¢ underdog limit
    t = TickContext(1, 0.50, 0.50, 0, 300)
    assert first_limit_hit(t, 0.10) is None
    assert first_limit_hit(t, 0.70) is None

    # Underdog: only when a side drops to ≤ 10¢
    t2 = TickContext(2, 0.08, 0.92, 10, 290)
    hit = first_limit_hit(t2, 0.10)
    assert hit == ("up", 0.08), hit

    # Favorite: only when a side rises to ≥ 70¢
    t3 = TickContext(3, 0.72, 0.28, 20, 280)
    hit3 = first_limit_hit(t3, 0.70)
    assert hit3 == ("up", 0.72), hit3

    s = Signull10Strategy({"threshold": 0.10})
    ctx = CandleContext("x", "t", 0, 300, "")
    # The first quote seeds crossing state. The cheap-side crossing occurs on
    # the following observation and uses its observed price, not 10¢.
    assert s.evaluate(t, ctx, entered=False) is None
    sig = s.evaluate(t2, ctx, entered=False)
    assert sig is not None and sig.side == "up" and sig.price == 0.08
    print("unit checks OK")

    candles = fetch_candles("btc", 120, use_cache=True)
    for thr in (0.10, 0.05, 0.70):
        r = run_backtest(
            get_strategy("signull_1_0", {"threshold": thr}),
            candles,
            initial_capital=100.0,
        )
        # Historical data is a price proxy. It may not claim a better price
        # than the threshold that triggered the side selection.
        if thr >= 0.5:
            assert all(t.entry_price >= thr for t in r.trades)
        else:
            assert all(t.entry_price <= thr for t in r.trades)
        print(
            f"thr={thr:.2f} trades={r.candles_traded}/{r.candles_loaded} "
            f"WR={r.win_rate:.1f}% end=${r.ending_capital:.2f} "
            f"ret={r.total_return_pct:+.1f}%"
        )


if __name__ == "__main__":
    main()
