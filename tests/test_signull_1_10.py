"""Tests for Signull 1.10 (Regime-Flip ±$10) strategy."""

from __future__ import annotations

import unittest

from strategies.base import CandleContext, TickContext
from strategies.signull_1_10 import Signull110Strategy, _log_return_std


def _candle() -> CandleContext:
    return CandleContext(slug="c1", title="c1", start_ts=3000, end_ts=3300, winner="up")


def _tick(*, up=0.60, down=0.40, t=3180) -> TickContext:
    return TickContext(
        t=t,
        up=up,
        down=down,
        seconds_into_candle=t - 3000,
        seconds_to_close=3300 - t,
        btc_price=111.0,           # +$11 vs the beat below -> triggers favor=UP
        btc_price_to_beat=100.0,
    )


class LogReturnStdTests(unittest.TestCase):
    def test_flat_series_has_zero_vol(self):
        self.assertEqual(_log_return_std([100.0] * 10, 0, 10), 0.0)

    def test_moving_series_has_positive_vol(self):
        closes = [100.0, 101.0, 100.0, 101.0, 100.0, 101.0]
        self.assertGreater(_log_return_std(closes, 0, 6), 0.0)

    def test_too_few_points_is_zero(self):
        self.assertEqual(_log_return_std([100.0], 0, 1), 0.0)


class Signull110RegimeTests(unittest.TestCase):
    def _prep(self, strategy: Signull110Strategy, closes: list[float]) -> None:
        strategy._kline_opens = [i * 60_000 for i in range(len(closes))]
        strategy._closes = list(closes)
        strategy._klines = []

    def test_calm_tape_follows_the_break(self):
        strat = Signull110Strategy()
        self._prep(strat, [100.0] * 60)  # no volatility -> calm -> follow

        signal = strat.evaluate(_tick(), _candle(), entered=False)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "up")
        self.assertIn("FOLLOW", signal.reason)
        self.assertIn("calm", signal.reason)

    def test_volatile_tape_inverts_the_break(self):
        strat = Signull110Strategy()
        closes = [100.0] * 23
        for i in range(37):
            closes.append(100.0 if i % 2 == 0 else 102.0)
        self._prep(strat, closes)  # violent chop -> volatile -> invert

        signal = strat.evaluate(_tick(), _candle(), entered=False)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "down")
        self.assertIn("INVERT", signal.reason)
        self.assertIn("volatile", signal.reason)

    def test_invert_when_volatile_can_be_disabled(self):
        strat = Signull110Strategy({"invert_when_volatile": False})
        closes = [100.0] * 23
        for i in range(37):
            closes.append(100.0 if i % 2 == 0 else 102.0)
        self._prep(strat, closes)

        signal = strat.evaluate(_tick(), _candle(), entered=False)
        self.assertIsNotNone(signal)
        assert signal is not None
        # Volatile regime but inversion disabled -> follow the UP break.
        self.assertEqual(signal.side, "up")

    def test_no_klines_falls_back_to_plain_1_5_behaviour(self):
        strat = Signull110Strategy()
        signal = strat.evaluate(_tick(), _candle(), entered=False)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "up")

    def test_waits_below_the_target(self):
        strat = Signull110Strategy()
        self._prep(strat, [100.0] * 60)
        below = TickContext(
            t=3180, up=0.55, down=0.45, seconds_into_candle=180, seconds_to_close=120,
            btc_price=105.0, btc_price_to_beat=100.0,
        )
        self.assertIsNone(strat.evaluate(below, _candle(), entered=False))

    def test_min_edge_not_applicable_but_sizing_label(self):
        strat = Signull110Strategy({"risk_pct": 0.10})
        self.assertEqual(strat.meta.default_params["invert_when_volatile"], True)


if __name__ == "__main__":
    unittest.main()
