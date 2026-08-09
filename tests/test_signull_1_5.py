"""Tests for Signull 1.5 strategy (BTC ±$30 Target)."""

from __future__ import annotations

import unittest

from strategies.base import CandleContext, TickContext, TradeSignal
from strategies.signull_1_5 import Signull15Strategy


class Signull15StrategyTests(unittest.TestCase):
    def test_evaluate_triggers_up_when_btc_rises_30_dollars(self):
        strategy = Signull15Strategy({"target_delta": 30.0})
        candle = CandleContext(slug="c1", title="c1", start_ts=1000, end_ts=1300, winner="up")

        # First tick at start of candle establishes start_btc = 90,000
        tick1 = TickContext(
            t=1000, up=0.50, down=0.50, seconds_into_candle=0, seconds_to_close=300, btc_price=90000.0
        )
        self.assertIsNone(strategy.evaluate(tick1, candle, entered=False))

        # BTC rises by $25 (+25) -> below $30 threshold -> no signal
        tick2 = TickContext(
            t=1010, up=0.60, down=0.40, seconds_into_candle=10, seconds_to_close=290, btc_price=90025.0
        )
        self.assertIsNone(strategy.evaluate(tick2, candle, entered=False))

        # BTC rises by $35 (+35 total from start) -> triggers UP signal
        tick3 = TickContext(
            t=1020, up=0.68, down=0.32, seconds_into_candle=20, seconds_to_close=280, btc_price=90035.0
        )
        signal = strategy.evaluate(tick3, candle, entered=False)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "up")
        self.assertAlmostEqual(signal.price, 0.68)
        self.assertIn("BTC reached +35.00$", signal.reason)

    def test_evaluate_triggers_down_when_btc_drops_30_dollars(self):
        strategy = Signull15Strategy({"target_delta": 30.0})
        candle = CandleContext(slug="c1", title="c1", start_ts=1000, end_ts=1300, winner="down")

        # Start BTC = 90,000
        tick1 = TickContext(
            t=1000, up=0.50, down=0.50, seconds_into_candle=0, seconds_to_close=300, btc_price=90000.0
        )
        self.assertIsNone(strategy.evaluate(tick1, candle, entered=False))

        # BTC drops by $32 (-32 total from start) -> triggers DOWN signal
        tick2 = TickContext(
            t=1015, up=0.35, down=0.65, seconds_into_candle=15, seconds_to_close=285, btc_price=89968.0
        )
        signal = strategy.evaluate(tick2, candle, entered=False)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "down")
        self.assertAlmostEqual(signal.price, 0.65)
        self.assertIn("BTC reached -32.00$", signal.reason)

    def test_evaluate_ignores_ticks_when_entered(self):
        strategy = Signull15Strategy({"target_delta": 30.0})
        candle = CandleContext(slug="c1", title="c1", start_ts=1000, end_ts=1300, winner="up")

        tick1 = TickContext(
            t=1000, up=0.50, down=0.50, seconds_into_candle=0, seconds_to_close=300, btc_price=90000.0
        )
        strategy.evaluate(tick1, candle, entered=False)

        tick2 = TickContext(
            t=1020, up=0.70, down=0.30, seconds_into_candle=20, seconds_to_close=280, btc_price=90050.0
        )
        self.assertIsNone(strategy.evaluate(tick2, candle, entered=True))

    def test_position_risk_fraction(self):
        strategy = Signull15Strategy({"risk_pct": 0.10})
        strategy.on_account_update(equity=200.0, initial_capital=100.0, peak_equity=200.0)

        signal = TradeSignal(side="up", price=0.60, reason="test")
        tick = TickContext(t=1, up=0.60, down=0.40, seconds_into_candle=10, seconds_to_close=290)
        candle = CandleContext(slug="c1", title="c1", start_ts=0, end_ts=300, winner="up")

        risk = strategy.position_risk_fraction(signal, tick, candle)
        # 10% of $200 equity = $20 -> 20% of initial $100 capital
        self.assertAlmostEqual(risk, 0.20)
        self.assertEqual(strategy.size_label(risk), "fixed-10%")

    def test_evaluate_handles_none_btc_price(self):
        strategy = Signull15Strategy({"target_delta": 30.0})
        candle = CandleContext(slug="c1", title="c1", start_ts=1000, end_ts=1300, winner="up")

        # Tick with btc_price = None should return None without error
        tick = TickContext(
            t=1000, up=0.50, down=0.50, seconds_into_candle=0, seconds_to_close=300, btc_price=None
        )
        self.assertIsNone(strategy.evaluate(tick, candle, entered=False))


if __name__ == "__main__":
    unittest.main()
