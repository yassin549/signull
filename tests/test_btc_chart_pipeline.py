"""BTC chart data pipeline — history always streams, uses fast spot, wall clock."""

from __future__ import annotations

import time
import unittest

from src.state import BotState


class BtcChartPipelineTests(unittest.TestCase):
    def test_history_without_beat_stores_absolute(self):
        state = BotState()
        t0 = time.time()
        state.update_btc_price(100_000.0, int(t0 * 1000))
        snap = state.get_snapshot(history_points=50)
        self.assertTrue(snap["btc_history"])
        pt = snap["btc_history"][-1]
        self.assertIn("v", pt)
        self.assertAlmostEqual(pt["v"], 100_000.0, places=0)
        # No beat yet → no d required
        self.assertIsNone(pt.get("d"))

    def test_history_uses_binance_not_only_chainlink(self):
        state = BotState()
        state.set_price_to_beat(100_000.0)
        # Chainlink lagging below; binance moves up (odds would follow binance)
        state.update_btc_chainlink(100_000.0, int(time.time() * 1000))
        time.sleep(0.06)
        state.update_btc_price(100_050.0, int(time.time() * 1000))
        snap = state.get_snapshot(history_points=50)
        # Live delta should follow binance
        self.assertAlmostEqual(snap["btc"]["delta"], 50.0, places=1)
        # Chart last point d from binance
        last = snap["btc_history"][-1]
        self.assertAlmostEqual(last["d"], 50.0, places=1)
        self.assertAlmostEqual(last["v"], 100_050.0, places=0)

    def test_wall_clock_timestamps_near_now(self):
        state = BotState()
        # RTDS ts far in the past should NOT pull chart timeline back
        old_ms = int((time.time() - 3600) * 1000)
        before = time.time()
        state.update_btc_price(99_000.0, old_ms)
        after = time.time()
        pt = state.get_snapshot(history_points=10)["btc_history"][-1]
        self.assertGreaterEqual(pt["t"], before - 0.5)
        self.assertLessEqual(pt["t"], after + 0.5)

    def test_set_beat_seeds_history_immediately(self):
        state = BotState()
        state.update_btc_price(50_000.0, int(time.time() * 1000))
        state.clear_market_data()
        # BTC samples are retained server-side; dashboard filters by candle.
        state.set_price_to_beat(50_010.0, candle_start_ts=1_700_000_000)
        state.update(
            market={
                "slug": "btc-updown-5m-1700000000",
                "candle_start_ts": 1_700_000_000,
            }
        )
        hist = state.get_snapshot(history_points=10)["btc_history"]
        self.assertTrue(hist)
        self.assertIn("d", hist[-1])
        self.assertEqual(hist[-1].get("cs"), 1_700_000_000)

    def test_clear_keeps_last_spot_for_reseed(self):
        state = BotState()
        state.update_btc_price(42_000.0, int(time.time() * 1000))
        state.clear_market_data()
        refs = state.get_resolution_refs()
        # Spot still available via internal last display after clear+sync
        snap = state.get_snapshot(history_points=0)
        # price may still be on snapshot from _sync after clear (beat None)
        # reseed path uses _last_btc_display
        state.set_price_to_beat(42_000.0)
        hist = state.get_snapshot(history_points=5)["btc_history"]
        self.assertTrue(len(hist) >= 1)
        self.assertAlmostEqual(hist[-1]["v"], 42_000.0, places=0)

    def test_version_bumps_on_price_move(self):
        state = BotState()
        v0 = state.version
        state.update_btc_price(1.0, int(time.time() * 1000))
        self.assertGreater(state.version, v0)

    def test_beat_locked_per_candle_not_overwritten(self):
        state = BotState()
        state.set_price_to_beat(100_000.0, candle_start_ts=1_700_000_000)
        # Later "bot" attempt with a different spot must not repin beat
        ok = state.set_price_to_beat(100_080.0, candle_start_ts=1_700_000_000)
        self.assertFalse(ok)
        refs = state.get_resolution_refs()
        self.assertAlmostEqual(refs["beat"], 100_000.0, places=1)
        # Moving spot still produces non-zero delta vs locked open
        time.sleep(0.06)
        state.update_btc_price(100_050.0, int(time.time() * 1000))
        snap = state.get_snapshot(history_points=20)
        self.assertAlmostEqual(snap["btc"]["delta"], 50.0, places=1)
        self.assertAlmostEqual(snap["btc"]["price_to_beat"], 100_000.0, places=1)

    def test_new_candle_can_set_new_beat(self):
        state = BotState()
        state.set_price_to_beat(100_000.0, candle_start_ts=100)
        state.clear_market_data(closing_start_ts=100)
        ok = state.set_price_to_beat(100_200.0, candle_start_ts=400)
        self.assertTrue(ok)
        self.assertAlmostEqual(state.get_resolution_refs()["beat"], 100_200.0, places=1)

    def test_snapshot_filters_btc_history_to_active_candle(self):
        state = BotState()
        state.update(
            market={"slug": "btc-new", "candle_start_ts": 200},
        )
        state.set_price_to_beat(50_000.0, candle_start_ts=200)
        state._btc_history.append({"t": 100.0, "v": 49_000.0, "cs": 100})
        state._btc_history.append({"t": 201.0, "v": 50_010.0, "cs": 200})
        hist = state.get_snapshot(history_points=10)["btc_history"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["cs"], 200)
        self.assertEqual(state.get_snapshot(history_points=10)["btc_candle_start_ts"], 200)

    def test_chainlink_fills_chart_when_binance_stale(self):
        state = BotState()
        state.set_price_to_beat(50_000.0, candle_start_ts=1)
        state.update_btc_price(50_000.0, int(time.time() * 1000))
        # Age out binance freshness
        state._binance_wall_ts = time.time() - 5.0
        time.sleep(0.06)
        state.update_btc_chainlink(50_040.0, int(time.time() * 1000))
        last = state.get_snapshot(history_points=10)["btc_history"][-1]
        self.assertAlmostEqual(last["v"], 50_040.0, places=0)
        self.assertAlmostEqual(last["d"], 40.0, places=1)

    def test_backfill_btc_history(self):
        state = BotState()
        cs = 1_700_000_000
        beat = 95_000.0
        now = time.time()
        history_points = [
            {"t": float(cs), "v": 95_000.0},
            {"t": float(cs + 60), "v": 95_050.0},
            {"t": float(cs + 120), "v": 95_020.0},
        ]
        state.backfill_btc_history(cs, beat, history_points)
        state.update(market={"candle_start_ts": cs})
        snap = state.get_snapshot(history_points=50)
        self.assertEqual(snap["btc"]["price_to_beat"], 95_000.0)
        self.assertEqual(len(snap["btc_history"]), 3)
        self.assertEqual(snap["btc_history"][0]["v"], 95_000.0)
        self.assertEqual(snap["btc_history"][0]["d"], 0.0)
        self.assertEqual(snap["btc_history"][1]["d"], 50.0)
        self.assertEqual(snap["btc_history"][2]["d"], 20.0)

    def test_backfill_price_history(self):
        state = BotState()
        cs = 1_700_000_000
        state.set_price_to_beat(95_000.0, candle_start_ts=cs)
        state.update(market={"candle_start_ts": cs})
        outcome_points = [
            {"t": float(cs), "up": 0.5, "down": 0.5},
            {"t": float(cs + 60), "up": 0.6, "down": 0.4},
        ]
        state.backfill_price_history(outcome_points)
        snap = state.get_snapshot(history_points=50)
        ph = snap["price_history"]
        self.assertEqual(len(ph), 2)
        self.assertEqual(ph[0]["up"], 0.5)
        self.assertEqual(ph[1]["up"], 0.6)


if __name__ == "__main__":
    unittest.main()


