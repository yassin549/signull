"""Tests for model_prob_threshold strategy."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from strategies.base import CandleContext, TickContext
from strategies.model_prob_threshold import ModelProbThresholdStrategy


class FakeStore:
    def __init__(self, series_by_start: dict[int, list[float]]):
        import numpy as np

        self._by = {k: np.asarray(v, dtype=float) for k, v in series_by_start.items()}
        self.n_loaded = len(self._by)
        self.source = "fake"

    def has_candle(self, start_ts: int) -> bool:
        return int(start_ts) in self._by

    def first_threshold_cross(self, start_ts, threshold, *, up_to_elapsed):
        import numpy as np

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

    def coverage(self, starts):
        hit = sum(1 for t in starts if self.has_candle(int(t)))
        return {
            "candles_requested": len(starts),
            "candles_with_probs": hit,
            "coverage": hit / len(starts) if starts else 0.0,
            "store_size": self.n_loaded,
            "source": self.source,
        }


class TestModelProbThreshold(unittest.TestCase):
    def setUp(self):
        # P(Up) rises and crosses 0.70 at second 5
        series = [0.5] * 5 + [0.72] + [0.75] * 294
        self.store = FakeStore({1_000_000: series})
        self.strategy = ModelProbThresholdStrategy({"threshold": 0.70, "risk_pct": 0.10})
        self.strategy._store = self.store

    def test_no_signal_before_cross(self):
        candle = CandleContext("s", "t", 1_000_000, 1_000_300, "up")
        tick = TickContext(t=1_000_003, up=0.55, down=0.45, seconds_into_candle=3, seconds_to_close=297)
        self.assertIsNone(self.strategy.evaluate(tick, candle, entered=False))

    def test_buys_up_when_model_crosses(self):
        candle = CandleContext("s", "t", 1_000_000, 1_000_300, "up")
        tick = TickContext(t=1_000_010, up=0.62, down=0.38, seconds_into_candle=10, seconds_to_close=290)
        sig = self.strategy.evaluate(tick, candle, entered=False)
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.side, "up")
        self.assertEqual(sig.price, 0.62)  # fill at market, not model p
        self.assertIn("70%", sig.reason)

    def test_ignores_crowd_for_decision(self):
        """Even if market Up is cheap, side follows model only."""
        # Force down: high P(Down)
        series = [0.2] * 300  # P(Up)=0.2 → P(Down)=0.8
        self.strategy._store = FakeStore({1_000_000: series})
        self.strategy._candle_state = {}
        candle = CandleContext("s2", "t", 1_000_000, 1_000_300, "down")
        tick = TickContext(t=1_000_050, up=0.90, down=0.10, seconds_into_candle=50, seconds_to_close=250)
        sig = self.strategy.evaluate(tick, candle, entered=False)
        self.assertIsNotNone(sig)
        assert sig is not None
        self.assertEqual(sig.side, "down")
        self.assertEqual(sig.price, 0.10)

    def test_one_signal_per_candle(self):
        candle = CandleContext("s", "t", 1_000_000, 1_000_300, "up")
        tick = TickContext(t=1_000_010, up=0.62, down=0.38, seconds_into_candle=10, seconds_to_close=290)
        self.assertIsNotNone(self.strategy.evaluate(tick, candle, entered=False))
        self.assertIsNone(self.strategy.evaluate(tick, candle, entered=False))

    def test_threshold_param(self):
        self.strategy = ModelProbThresholdStrategy({"threshold": 0.90, "risk_pct": 0.10})
        self.strategy._store = self.store
        candle = CandleContext("s", "t", 1_000_000, 1_000_300, "up")
        tick = TickContext(t=1_000_010, up=0.62, down=0.38, seconds_into_candle=10, seconds_to_close=290)
        # series only reaches 0.75 — should not fire at 0.90
        self.assertIsNone(self.strategy.evaluate(tick, candle, entered=False))


if __name__ == "__main__":
    unittest.main()
