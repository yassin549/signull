"""Tests for Signull 1.6 strategy (Option SIM 1.1 probability target)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from src.backtest.registry import get_strategy, list_strategies
from strategies.base import CandleContext, TickContext
from strategies.signull_1_6 import Signull16Strategy


class FakeStore:
    def __init__(self, series_by_start: dict[int, list[float]]):
        import numpy as np

        self._by = {k: np.asarray(v, dtype=float) for k, v in series_by_start.items()}
        self.n_loaded = len(self._by)
        self.source = "fake"
        self.first_ts = 1_000_000
        self.last_ts = 1_000_300

    def has_candle(self, start_ts: int) -> bool:
        return int(start_ts) in self._by

    def first_threshold_cross(self, start_ts, threshold, *, up_to_elapsed):
        series = self._by.get(int(start_ts))
        if series is None:
            return None
        thr = float(threshold)
        last = min(int(up_to_elapsed), len(series) - 1)
        for s in range(0, last + 1):
            p_up = float(series[s])
            p_down = 1.0 - p_up
            if p_up >= thr:
                return ("up", s, p_up)
            if p_down >= thr:
                return ("down", s, p_down)
        return None

    def time_range_iso(self):
        return ("2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z")

    def coverage(self, starts):
        hit = sum(1 for t in starts if self.has_candle(int(t)))
        return {
            "candles_requested": len(starts),
            "candles_with_probs": hit,
            "coverage": hit / len(starts) if starts else 0.0,
            "store_size": self.n_loaded,
            "source": self.source,
        }


class TestSignull16Strategy(unittest.TestCase):
    def setUp(self):
        # P(Up) rises and crosses default 0.65 (65%) at second 5
        series = [0.5] * 5 + [0.68] + [0.70] * 294
        self.store = FakeStore({1_000_000: series})
        self.strategy = Signull16Strategy({"threshold": 0.65, "risk_pct": 0.10})
        self.strategy._store = self.store

    def test_registered_in_backtest_registry(self):
        strats = list_strategies()
        ids = [s["id"] for s in strats]
        self.assertIn("signull_1_6", ids)
        strat_obj = get_strategy("signull_1_6")
        self.assertEqual(strat_obj.meta.id, "signull_1_6")
        self.assertEqual(type(strat_obj).__name__, "Signull16Strategy")

    def test_dynamic_threshold_parsing(self):
        # Passing 65 or 0.65 should normalize to 0.65
        s1 = Signull16Strategy({"threshold": 65})
        self.assertEqual(s1.params["threshold"], 0.65)

        s2 = Signull16Strategy({"threshold": 0.75})
        self.assertEqual(s2.params["threshold"], 0.75)

    def test_no_signal_before_cross(self):
        candle = CandleContext("s", "t", 1_000_000, 1_000_300, "up")
        tick = TickContext(t=1_000_003, up=0.55, down=0.45, seconds_into_candle=3, seconds_to_close=297)
        self.assertIsNone(self.strategy.evaluate(tick, candle, entered=False))

    def test_buys_up_when_model_crosses_65_pct(self):
        candle = CandleContext("s", "t", 1_000_000, 1_000_300, "up")
        tick = TickContext(t=1_000_010, up=0.60, down=0.40, seconds_into_candle=10, seconds_to_close=290)
        sig = self.strategy.evaluate(tick, candle, entered=False)
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.side, "up")
        self.assertEqual(sig.price, 0.60)  # market fill
        self.assertIn("65%", sig.reason)
        self.assertIn("Option SIM 1.1", sig.reason)

    def test_buys_down_when_model_down_prob_crosses(self):
        # P(Up) = 0.20 -> P(Down) = 0.80 >= 0.65
        series = [0.20] * 300
        self.strategy._store = FakeStore({1_000_000: series})
        self.strategy._candle_state = {}
        candle = CandleContext("s2", "t", 1_000_000, 1_000_300, "down")
        tick = TickContext(t=1_000_050, up=0.85, down=0.15, seconds_into_candle=50, seconds_to_close=250)
        sig = self.strategy.evaluate(tick, candle, entered=False)
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.side, "down")
        self.assertEqual(sig.price, 0.15)
        self.assertIn("DOWN", sig.reason)

    def test_one_signal_per_candle(self):
        candle = CandleContext("s", "t", 1_000_000, 1_000_300, "up")
        tick = TickContext(t=1_000_010, up=0.60, down=0.40, seconds_into_candle=10, seconds_to_close=290)
        self.assertIsNotNone(self.strategy.evaluate(tick, candle, entered=False))
        self.assertIsNone(self.strategy.evaluate(tick, candle, entered=False))

    def test_position_risk_fraction(self):
        candle = CandleContext("s", "t", 1_000_000, 1_000_300, "up")
        tick = TickContext(t=1_000_010, up=0.60, down=0.40, seconds_into_candle=10, seconds_to_close=290)
        self.strategy._equity = 100.0
        self.strategy._initial_capital = 100.0
        sig = self.strategy.evaluate(tick, candle, entered=False)
        assert sig is not None
        rf = self.strategy.position_risk_fraction(sig, tick, candle)
        self.assertAlmostEqual(rf, 0.10)
        self.assertEqual(self.strategy.size_label(rf), "sim1.1≥65%")


if __name__ == "__main__":
    unittest.main()
