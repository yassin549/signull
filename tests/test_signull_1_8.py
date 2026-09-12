"""Tests for Signull 1.8 (Open Favorite) strategy."""

from __future__ import annotations

import unittest

from strategies.base import CandleContext, TickContext, TradeSignal
from strategies.signull_1_8 import Signull18Strategy


class Signull18StrategyTests(unittest.TestCase):
    def test_default_params(self):
        self.assertEqual(Signull18Strategy.meta.default_params["risk_pct"], 0.10)
        self.assertEqual(Signull18Strategy.meta.default_params["min_edge"], 0.0)

    def test_buys_up_when_up_is_favored(self):
        strategy = Signull18Strategy()
        candle = CandleContext(slug="c1", title="c1", start_ts=0, end_ts=300, winner="up")
        tick = TickContext(t=1, up=0.51, down=0.49, seconds_into_candle=1, seconds_to_close=299)

        signal = strategy.evaluate(tick, candle, entered=False)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "up")
        self.assertAlmostEqual(signal.price, 0.51)
        self.assertIn("Open favorite UP", signal.reason)

    def test_buys_down_when_down_is_favored(self):
        strategy = Signull18Strategy()
        candle = CandleContext(slug="c1", title="c1", start_ts=0, end_ts=300, winner="down")
        tick = TickContext(t=1, up=0.46, down=0.54, seconds_into_candle=1, seconds_to_close=299)

        signal = strategy.evaluate(tick, candle, entered=False)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "down")
        self.assertAlmostEqual(signal.price, 0.54)

    def test_waits_on_a_dead_heat(self):
        strategy = Signull18Strategy()
        candle = CandleContext(slug="c1", title="c1", start_ts=0, end_ts=300, winner="up")
        tick = TickContext(t=1, up=0.50, down=0.50, seconds_into_candle=1, seconds_to_close=299)
        self.assertIsNone(strategy.evaluate(tick, candle, entered=False))

    def test_min_edge_blocks_small_lean(self):
        strategy = Signull18Strategy({"min_edge": 0.05})
        candle = CandleContext(slug="c1", title="c1", start_ts=0, end_ts=300, winner="up")
        small = TickContext(t=1, up=0.52, down=0.48, seconds_into_candle=1, seconds_to_close=299)
        self.assertIsNone(strategy.evaluate(small, candle, entered=False))

        strong = TickContext(t=2, up=0.58, down=0.42, seconds_into_candle=2, seconds_to_close=298)
        signal = strategy.evaluate(strong, candle, entered=False)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "up")

    def test_ignores_ticks_when_entered(self):
        strategy = Signull18Strategy()
        candle = CandleContext(slug="c1", title="c1", start_ts=0, end_ts=300, winner="up")
        tick = TickContext(t=1, up=0.60, down=0.40, seconds_into_candle=1, seconds_to_close=299)
        self.assertIsNone(strategy.evaluate(tick, candle, entered=True))

    def test_ignores_invalid_prices(self):
        strategy = Signull18Strategy()
        candle = CandleContext(slug="c1", title="c1", start_ts=0, end_ts=300, winner="up")
        tick = TickContext(t=1, up=1.0, down=0.0, seconds_into_candle=1, seconds_to_close=299)
        self.assertIsNone(strategy.evaluate(tick, candle, entered=False))

    def test_position_risk_fraction(self):
        strategy = Signull18Strategy({"risk_pct": 0.10})
        strategy.on_account_update(equity=200.0, initial_capital=100.0, peak_equity=200.0)

        signal = TradeSignal(side="up", price=0.60, reason="test")
        tick = TickContext(t=1, up=0.60, down=0.40, seconds_into_candle=1, seconds_to_close=299)
        candle = CandleContext(slug="c1", title="c1", start_ts=0, end_ts=300, winner="up")

        risk = strategy.position_risk_fraction(signal, tick, candle)
        # 10% of $200 equity = $20 -> 20% of initial $100 capital
        self.assertAlmostEqual(risk, 0.20)
        self.assertEqual(strategy.size_label(risk), "fixed-10%")


if __name__ == "__main__":
    unittest.main()
