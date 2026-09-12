"""Tests for Signull 1.7 adaptive BTC point-of-no-return strategy."""

from __future__ import annotations

import unittest

from src.backtest.registry import get_strategy, list_strategies
from strategies.base import CandleContext, TickContext
from strategies.signull_1_7 import Signull17Strategy


def _bars(start_ts: int, prices: list[float], *, range_width: float) -> list[tuple[int, float, float, float, float, float]]:
    out = []
    for i, price in enumerate(prices):
        open_time_ms = (start_ts + i * 60) * 1000
        open_px = prices[i - 1] if i else price
        close_px = price
        high = max(open_px, close_px) + range_width / 2.0
        low = min(open_px, close_px) - range_width / 2.0
        out.append((open_time_ms, open_px, high, low, close_px, 1.0))
    return out


class Signull17StrategyTests(unittest.TestCase):
    def test_registered_in_backtest_registry(self):
        ids = [s["id"] for s in list_strategies()]
        self.assertIn("signull_1_7", ids)
        strategy = get_strategy("signull_1_7")
        self.assertEqual(strategy.meta.id, "signull_1_7")
        self.assertEqual(type(strategy).__name__, "Signull17Strategy")

    def test_threshold_increases_in_high_volatility_regime(self):
        candle_start = 10_000
        low_prices = [90_000 + i for i in range(70)]
        high_prices = [90_000 + ((-1) ** i) * 75 for i in range(70)]

        low = Signull17Strategy({"lookback_minutes": 60, "min_delta": 1.0, "max_delta": 500.0})
        low._klines = _bars(candle_start - 70 * 60, low_prices, range_width=3.0)
        low._kline_opens = [k[0] for k in low._klines]

        high = Signull17Strategy({"lookback_minutes": 60, "min_delta": 1.0, "max_delta": 500.0})
        high._klines = _bars(candle_start - 70 * 60, high_prices, range_width=150.0)
        high._kline_opens = [k[0] for k in high._klines]

        low_threshold, _ = low._dynamic_threshold(candle_start, 90_000.0)
        high_threshold, _ = high._dynamic_threshold(candle_start, 90_000.0)

        self.assertGreater(high_threshold, low_threshold)

    def test_threshold_multiplier_makes_entries_more_tolerant(self):
        candle_start = 10_000
        prices = [90_000 + ((-1) ** i) * 30 for i in range(70)]

        strict = Signull17Strategy({
            "lookback_minutes": 60,
            "min_delta": 1.0,
            "max_delta": 500.0,
            "threshold_multiplier": 1.0,
        })
        strict._klines = _bars(candle_start - 70 * 60, prices, range_width=60.0)
        strict._kline_opens = [k[0] for k in strict._klines]

        tolerant = Signull17Strategy({
            "lookback_minutes": 60,
            "min_delta": 1.0,
            "max_delta": 500.0,
            "threshold_multiplier": 0.70,
        })
        tolerant._klines = list(strict._klines)
        tolerant._kline_opens = list(strict._kline_opens)

        strict_threshold, _ = strict._dynamic_threshold(candle_start, 90_000.0)
        tolerant_threshold, _ = tolerant._dynamic_threshold(candle_start, 90_000.0)

        self.assertLess(tolerant_threshold, strict_threshold)

    def test_threshold_is_frozen_for_each_candle_after_first_tick(self):
        candle_start = 10_000
        prices = [90_000 + ((-1) ** i) * 15 for i in range(70)]
        strategy = Signull17Strategy({
            "lookback_minutes": 60,
            "min_delta": 5.0,
            "max_delta": 200.0,
            "target_reversal_prob": 0.25,
        })
        strategy._klines = _bars(candle_start - 70 * 60, prices, range_width=30.0)
        strategy._kline_opens = [k[0] for k in strategy._klines]
        candle = CandleContext("c1", "c1", candle_start, candle_start + 300, "up")

        first = TickContext(
            t=candle_start,
            up=0.50,
            down=0.50,
            seconds_into_candle=0,
            seconds_to_close=300,
            btc_price=90_000.0,
        )
        self.assertIsNone(strategy.evaluate(first, candle, entered=False))
        frozen = strategy._candle_state["c1"]["threshold"]

        strategy.params["min_delta"] = 150.0
        strategy.params["max_delta"] = 150.0
        strategy._klines = _bars(candle_start - 70 * 60, [90_000 + ((-1) ** i) * 400 for i in range(70)], range_width=800.0)
        strategy._kline_opens = [k[0] for k in strategy._klines]

        below = TickContext(
            t=candle_start + 20,
            up=0.55,
            down=0.45,
            seconds_into_candle=20,
            seconds_to_close=280,
            btc_price=90_000.0 + frozen - 0.01,
        )
        self.assertIsNone(strategy.evaluate(below, candle, entered=False))
        self.assertEqual(strategy._candle_state["c1"]["threshold"], frozen)

        above = TickContext(
            t=candle_start + 30,
            up=0.62,
            down=0.38,
            seconds_into_candle=30,
            seconds_to_close=270,
            btc_price=90_000.0 + frozen + 0.01,
        )
        signal = strategy.evaluate(above, candle, entered=False)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "up")
        self.assertAlmostEqual(signal.price, 0.62)
        self.assertIn("adaptive PNR", signal.reason)

    def test_triggers_down_when_dynamic_threshold_is_crossed(self):
        candle_start = 20_000
        strategy = Signull17Strategy({"min_delta": 12.0, "max_delta": 12.0})
        candle = CandleContext("c2", "c2", candle_start, candle_start + 300, "down")

        first = TickContext(
            t=candle_start,
            up=0.50,
            down=0.50,
            seconds_into_candle=0,
            seconds_to_close=300,
            btc_price=90_000.0,
        )
        self.assertIsNone(strategy.evaluate(first, candle, entered=False))

        tick = TickContext(
            t=candle_start + 10,
            up=0.40,
            down=0.60,
            seconds_into_candle=10,
            seconds_to_close=290,
            btc_price=89_987.0,
        )
        signal = strategy.evaluate(tick, candle, entered=False)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "down")
        self.assertAlmostEqual(signal.price, 0.60)


if __name__ == "__main__":
    unittest.main()
