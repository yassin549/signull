"""Unit tests for live-mirrored backtest sizing (fixed USD vs % of bankroll)."""

from __future__ import annotations

import unittest

from strategies.base import Strategy, StrategyMeta, TradeSignal
from src.backtest.engine import run_backtest
from src.backtest.types import CandleDataset
from src.backtest_server import BacktestRequest


class DummyStrategy(Strategy):
    meta = StrategyMeta(id="dummy", name="Dummy", description="Dummy test strategy")

    def evaluate(self, _tick, candle, *, entered):
        if entered:
            return None
        return TradeSignal(side="up", price=0.50, reason="test signal")

    def position_risk_fraction(self, *_args):
        return 0.10


class CompoundingStrategy(DummyStrategy):
    """Mirrors signull_1_5: risk a fixed % of *current* equity."""

    def position_risk_fraction(self, *_args):
        return (self._equity * 0.10) / max(self._initial_capital, 1e-9)


def make_test_candles(count: int = 5) -> list[CandleDataset]:
    candles = []
    for i in range(1, count + 1):
        candles.append(
            CandleDataset(
                slug=f"candle_{i}",
                title=f"Candle {i}",
                start_ts=i * 300,
                end_ts=(i + 1) * 300,
                winner="up",  # All trades win
                up_token_id="u",
                down_token_id="d",
                # Entry print at 0.50, then a later print at 0.50 so the
                # live-style limit buy actually fills.
                ticks=[(i * 300 + 1, 0.50, 0.50), (i * 300 + 5, 0.50, 0.50)],
            )
        )
    return candles


class TestBacktestSizing(unittest.TestCase):
    def test_percent_of_account_balance_compounds(self):
        candles = make_test_candles(3)
        result = run_backtest(
            CompoundingStrategy(),
            candles,
            initial_capital=100.0,
            taker_fee_rate=0.0,
        )

        trades = result.trades
        self.assertEqual(len(trades), 3)
        self.assertAlmostEqual(trades[0].stake, 10.0, places=2)
        # 10% of $110 after the first win.
        self.assertAlmostEqual(trades[1].stake, 11.0, places=2)
        self.assertAlmostEqual(trades[2].stake, 12.1, places=2)

    def test_fixed_dollar_amount_stays_constant(self):
        candles = make_test_candles(3)
        result = run_backtest(
            DummyStrategy(),
            candles,
            initial_capital=100.0,
            use_fixed_stake=True,
            fixed_stake_usdc=15.0,
            taker_fee_rate=0.0,
        )

        trades = result.trades
        self.assertEqual(len(trades), 3)
        for trade in trades:
            self.assertAlmostEqual(trade.stake, 15.0, places=2)
        self.assertAlmostEqual(result.ending_capital, 145.0, places=2)
        self.assertEqual(trades[0].size_label, "$15.00 fixed")

    def test_fixed_dollar_amount_capped_by_capital(self):
        candles = make_test_candles(1)
        result = run_backtest(
            DummyStrategy(),
            candles,
            initial_capital=20.0,
            use_fixed_stake=True,
            fixed_stake_usdc=50.0,
            taker_fee_rate=0.0,
        )

        trades = result.trades
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].stake, 20.0, places=2)

    def test_backtest_request_defaults(self):
        req = BacktestRequest(strategy_id="dummy")
        self.assertEqual(req.asset, "btc")
        self.assertEqual(req.initial_capital, 100.0)


if __name__ == "__main__":
    unittest.main()
