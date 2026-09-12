"""Live-style limit fill reconstruction used by the single-book backtest."""

from __future__ import annotations

import unittest

from src.backtest.engine import run_backtest
from src.backtest.fills import (
    REASON_CROSSED,
    REASON_FILLED,
    REASON_LATE,
    REASON_NO_RETRACE,
    REASON_NO_TICKS,
    simulate_live_limit_buy,
)
from src.backtest.types import CandleDataset
from strategies.base import Strategy, StrategyMeta, TradeSignal


class AlwaysUp(Strategy):
    meta = StrategyMeta(id="always_up", name="always_up", description="always_up")

    def evaluate(self, tick, _candle, *, entered):
        if entered:
            return None
        if tick.up < 0.70:
            return None
        return TradeSignal("up", 0.70, "test")

    def position_risk_fraction(self, *_args):
        return 0.10


def _candle(ticks, winner="up", start=0, end=300) -> CandleDataset:
    return CandleDataset("c", "c", start, end, winner, "u", "d", ticks)


class SimulateLiveLimitBuyTests(unittest.TestCase):
    def test_later_print_at_or_through_limit_fills(self):
        fill = simulate_live_limit_buy(
            side="up",
            limit_price=0.70,
            entry_ts=10,
            seconds_to_close=290,
            ticks=[(10, 0.72, 0.28), (40, 0.68, 0.32)],
            end_ts=300,
        )
        self.assertTrue(fill.filled)
        self.assertEqual(fill.reason, REASON_FILLED)
        self.assertEqual(fill.fill_ts, 40)

    def test_price_running_away_does_not_fill(self):
        fill = simulate_live_limit_buy(
            side="up",
            limit_price=0.70,
            entry_ts=10,
            seconds_to_close=290,
            ticks=[(10, 0.70, 0.30), (40, 0.78, 0.22), (90, 0.91, 0.09)],
            end_ts=300,
        )
        self.assertFalse(fill.filled)
        self.assertEqual(fill.reason, REASON_NO_RETRACE)
        self.assertAlmostEqual(fill.worst_price, 0.78)

    def test_signal_tick_alone_is_not_a_fill(self):
        fill = simulate_live_limit_buy(
            side="up",
            limit_price=0.70,
            entry_ts=10,
            seconds_to_close=290,
            ticks=[(10, 0.70, 0.30)],
            end_ts=300,
        )
        self.assertFalse(fill.filled)
        self.assertEqual(fill.reason, REASON_NO_TICKS)

    def test_already_through_the_market_takes_immediately(self):
        fill = simulate_live_limit_buy(
            side="up",
            limit_price=0.70,
            entry_ts=10,
            seconds_to_close=290,
            ticks=[(10, 0.61, 0.39)],
            end_ts=300,
        )
        self.assertTrue(fill.filled)
        self.assertEqual(fill.reason, REASON_CROSSED)

    def test_through_at_submit_fills_even_if_later_prints_run_away(self):
        fill = simulate_live_limit_buy(
            side="up",
            limit_price=0.70,
            entry_ts=10,
            seconds_to_close=290,
            ticks=[(10, 0.61, 0.39), (40, 0.88, 0.12)],
            end_ts=300,
        )
        self.assertTrue(fill.filled)
        self.assertEqual(fill.reason, REASON_CROSSED)

    def test_late_candle_is_rejected(self):
        fill = simulate_live_limit_buy(
            side="up",
            limit_price=0.70,
            entry_ts=295,
            seconds_to_close=5,
            ticks=[(295, 0.70, 0.30), (298, 0.65, 0.35)],
            end_ts=300,
        )
        self.assertFalse(fill.filled)
        self.assertEqual(fill.reason, REASON_LATE)

    def test_down_side_uses_down_prints(self):
        fill = simulate_live_limit_buy(
            side="down",
            limit_price=0.40,
            entry_ts=10,
            seconds_to_close=290,
            ticks=[(10, 0.62, 0.41), (50, 0.70, 0.38)],
            end_ts=300,
        )
        self.assertTrue(fill.filled)
        self.assertEqual(fill.reason, REASON_FILLED)


class SingleBookBacktestTests(unittest.TestCase):
    def test_unfilled_signal_is_not_a_trade_or_loss(self):
        candle = _candle(
            [(0, 0.50, 0.50), (10, 0.72, 0.28), (80, 0.88, 0.12)],
            winner="up",
        )
        result = run_backtest(AlwaysUp(), [candle], initial_capital=100.0)

        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertFalse(trade.filled)
        self.assertEqual(trade.fill_reason, REASON_NO_RETRACE)
        self.assertEqual(trade.pnl, 0.0)
        self.assertAlmostEqual(result.ending_capital, 100.0, places=2)
        self.assertEqual(result.candles_traded, 0)
        self.assertEqual(result.unfilled, 1)
        self.assertEqual(result.fills, 0)
        self.assertEqual(result.fill_rate, 0.0)
        self.assertTrue(result.equity_curve[-1].get("unfilled"))

    def test_retrace_fills_and_settles(self):
        candle = _candle(
            [(0, 0.50, 0.50), (10, 0.72, 0.28), (80, 0.66, 0.34)],
            winner="up",
        )
        result = run_backtest(AlwaysUp(), [candle], initial_capital=100.0)

        trade = result.trades[0]
        self.assertTrue(trade.filled)
        self.assertEqual(trade.fill_reason, REASON_FILLED)
        self.assertGreater(trade.pnl, 0)
        self.assertAlmostEqual(result.ending_capital, 100.0 + trade.pnl, places=2)
        self.assertEqual(result.fill_rate, 100.0)
        self.assertEqual(result.fills, 1)

    def test_unfilled_loss_does_not_show_as_a_loss(self):
        win = _candle(
            [(0, 0.50, 0.50), (10, 0.71, 0.29), (90, 0.60, 0.40)],
            winner="up",
        )
        miss_loss = _candle(
            [(300, 0.50, 0.50), (310, 0.71, 0.29), (380, 0.90, 0.10)],
            winner="down",
            start=300,
            end=600,
        )
        result = run_backtest(AlwaysUp(), [win, miss_loss], initial_capital=100.0)

        self.assertEqual(result.candles_traded, 1)
        self.assertEqual(result.wins, 1)
        self.assertEqual(result.losses, 0)
        self.assertEqual(result.fills, 1)
        self.assertEqual(result.unfilled, 1)

    def test_to_dict_exposes_single_book_fill_stats(self):
        candle = _candle([(0, 0.50, 0.50), (10, 0.80, 0.20)])
        payload = run_backtest(AlwaysUp(), [candle]).to_dict()
        self.assertNotIn("live", payload)
        for key in ("fills", "unfilled", "fill_rate", "equity_curve", "ending_capital"):
            self.assertIn(key, payload)


if __name__ == "__main__":
    unittest.main()
