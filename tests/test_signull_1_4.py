"""Tests for Signull 1.4 strategy."""

from __future__ import annotations

import unittest

from strategies.base import CandleContext, TickContext, TradeSignal
from strategies.signull_1_4 import Signull14Strategy


class Signull14StrategyTests(unittest.TestCase):
    def test_evaluate_triggers_on_first_side_reaching_threshold(self):
        strategy = Signull14Strategy({"threshold": 0.70})
        candle = CandleContext(slug="c1", title="c1", start_ts=0, end_ts=300, winner="up")

        # Ticks below threshold -> no signal
        tick1 = TickContext(t=1, up=0.55, down=0.45, seconds_into_candle=10, seconds_to_close=290)
        self.assertIsNone(strategy.evaluate(tick1, candle, entered=False))

        # UP reaches 0.70 first -> signals UP entry @ 0.70
        tick2 = TickContext(t=2, up=0.71, down=0.29, seconds_into_candle=20, seconds_to_close=280)
        signal = strategy.evaluate(tick2, candle, entered=False)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "up")
        self.assertAlmostEqual(signal.price, 0.71)

    def test_evaluate_ignores_ticks_when_already_entered(self):
        strategy = Signull14Strategy({"threshold": 0.70})
        candle = CandleContext(slug="c1", title="c1", start_ts=0, end_ts=300, winner="up")
        tick = TickContext(t=2, up=0.75, down=0.25, seconds_into_candle=20, seconds_to_close=280)

        self.assertIsNone(strategy.evaluate(tick, candle, entered=True))

    def test_position_risk_fraction_uses_fixed_10_percent(self):
        strategy = Signull14Strategy({"risk_pct": 0.10})
        strategy.on_account_update(equity=150.0, initial_capital=100.0, peak_equity=150.0)

        signal = TradeSignal(side="up", price=0.70, reason="test")
        tick = TickContext(t=1, up=0.70, down=0.30, seconds_into_candle=10, seconds_to_close=290)
        candle = CandleContext(slug="c1", title="c1", start_ts=0, end_ts=300, winner="up")

        # 10% of current equity ($150) = $15, which is 15% of initial capital ($100)
        risk = strategy.position_risk_fraction(signal, tick, candle)
        self.assertAlmostEqual(risk, 0.15)
        self.assertEqual(strategy.size_label(risk), "fixed-10%")

    def test_position_risk_fraction_unaffected_by_drawdown_or_losses(self):
        strategy = Signull14Strategy({"risk_pct": 0.10})
        # Account is in deep drawdown ($60 equity vs $100 peak) with losses streak
        strategy.on_account_update(
            equity=60.0,
            initial_capital=100.0,
            peak_equity=100.0,
            losses_streak=5,
        )

        signal = TradeSignal(side="up", price=0.70, reason="test")
        tick = TickContext(t=1, up=0.70, down=0.30, seconds_into_candle=10, seconds_to_close=290)
        candle = CandleContext(slug="c1", title="c1", start_ts=0, end_ts=300, winner="up")

        # Exactly 10% of $60 equity = $6 (6% of initial capital)
        risk = strategy.position_risk_fraction(signal, tick, candle)
        self.assertAlmostEqual(risk, 0.06)
        self.assertEqual(strategy.size_label(risk), "fixed-10%")


if __name__ == "__main__":
    unittest.main()
