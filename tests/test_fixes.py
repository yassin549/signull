"""Regression tests for analysis-found bugs (drive shipped modules)."""

from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from src.bot import PendingTrade, TradingBot, _SettleResult, _settle_pnl
from src.config import BotConfig
from src.ml import btc_features
from src.ml.btc_features import window_features
from src.polymarket import PolymarketClient
from src.sizing import compute_stake, partial_fill_stake, scale_pending_for_fill
from src.state import BotState, slice_history_tail


def _paper_config(**overrides) -> BotConfig:
    base = dict(
        trading_mode="paper",
        asset="btc",
        order_size_usdc=5.0,
        max_entry_price=0.55,
        poll_interval_sec=2.0,
        private_key=None,
        funder_address=None,
        signature_type=1,
        server_host="127.0.0.1",
        server_port=8080,
        dashboard_push_ms=50,
        bot_poll_interval_sec=2.0,
        paper_initial_capital=100.0,
        strategy_threshold=0.70,
        strategy_min_risk_pct=0.05,
        strategy_max_risk_pct=0.50,
        strategy_trust_lookback=3,
        strategy_btc_align_min=0.55,
        strategy_big_equity_buffer=1.25,
    )
    base.update(overrides)
    return BotConfig(**base)


class SliceHistoryTests(unittest.TestCase):
    def test_non_positive_returns_empty(self):
        data = [{"t": i} for i in range(50)]
        self.assertEqual(slice_history_tail(data, 0), [])
        self.assertEqual(slice_history_tail(data, -1), [])
        self.assertEqual(slice_history_tail(data, None), [])  # type: ignore[arg-type]

    def test_positive_tail(self):
        data = list(range(10))
        self.assertEqual(slice_history_tail(data, 3), [7, 8, 9])

    def test_get_snapshot_history_points_zero(self):
        state = BotState()
        # Seed history via public API
        state.update_feed_best("up", 0.4, 0.6)
        state.update_btc_price(100.0, int(time.time() * 1000))
        state.set_price_to_beat(99.0)
        state.update_btc_chainlink(100.5, int(time.time() * 1000))
        # Force history append by sleeping past interval
        time.sleep(0.06)
        state.update_feed_best("up", 0.41, 0.61)
        state.update_btc_price(100.1, int(time.time() * 1000))

        snap = state.get_snapshot(history_points=0)
        self.assertEqual(snap["price_history"], [])
        self.assertEqual(snap["btc_history"], [])

        # Positive still works
        snap2 = state.get_snapshot(history_points=600)
        self.assertIsInstance(snap2["price_history"], list)


class ClientStartupTests(unittest.TestCase):
    def test_paper_mode_never_derives_wallet_credentials(self):
        """Paper dashboard startup must not depend on the CLOB auth endpoint."""
        config = _paper_config(
            private_key="0x" + "1" * 64,
            funder_address="0x" + "2" * 40,
        )
        with mock.patch("src.polymarket.ClobClient") as client_cls:
            client = PolymarketClient(config)

        self.assertFalse(client.is_authenticated)
        client_cls.assert_called_once()
        client_cls.return_value.create_or_derive_api_key.assert_not_called()

    def test_live_auth_failure_falls_back_to_public_market_client(self):
        config = _paper_config(
            trading_mode="live",
            private_key="0x" + "1" * 64,
            funder_address="0x" + "2" * 40,
        )
        with mock.patch("src.polymarket.ClobClient") as client_cls:
            client_cls.return_value.create_or_derive_api_key.side_effect = RuntimeError("timeout")
            client = PolymarketClient(config)

        self.assertFalse(client.is_authenticated)
        self.assertEqual(client.auth_error, "timeout")
        self.assertEqual(client_cls.call_count, 2)


class MarketFeedResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_failure_is_a_warning_not_a_traceback(self):
        from src.feed import MarketFeed

        feed = MarketFeed(_paper_config(), BotState())
        feed._client.get_order_book = mock.Mock(side_effect=RuntimeError("disconnected"))

        with self.assertLogs("src.feed", level="WARNING") as logs:
            await feed._bootstrap_book("token", "up")

        self.assertIn("REST book bootstrap unavailable for up: disconnected", logs.output[0])


class ResolutionRefsTests(unittest.TestCase):
    def test_freeze_survives_clear_and_new_beat(self):
        state = BotState()
        state.set_price_to_beat(50_000.0)
        state.update_btc_price(50_010.0, 1_700_000_000_000)
        state.update_btc_chainlink(50_005.0, 1_700_000_000_100)

        closed_start = 1_700_000_000
        frozen = state.freeze_resolution_refs(closed_start)
        self.assertEqual(frozen["beat"], 50_000.0)
        self.assertEqual(frozen["chainlink"], 50_005.0)
        self.assertEqual(frozen["spot"], 50_010.0)

        # Simulate feed rollover: clear + new open beat
        state.clear_market_data(closing_start_ts=closed_start)
        state.set_price_to_beat(51_000.0)
        state.update_btc_chainlink(51_100.0, 1_700_000_300_000)
        state.update_btc_price(51_200.0, 1_700_000_300_100)

        # Live refs now show the *new* candle
        live = state.get_resolution_refs()
        self.assertEqual(live["beat"], 51_000.0)
        self.assertEqual(live["chainlink"], 51_100.0)

        # Frozen closed-window refs are unchanged
        again = state.get_frozen_resolution_refs(closed_start)
        self.assertIsNotNone(again)
        assert again is not None
        self.assertEqual(again["beat"], 50_000.0)
        self.assertEqual(again["chainlink"], 50_005.0)
        self.assertEqual(again["spot"], 50_010.0)

    def test_first_freeze_wins(self):
        state = BotState()
        state.set_price_to_beat(100.0)
        state.update_btc_chainlink(101.0, 1000)
        state.freeze_resolution_refs(42)
        state.set_price_to_beat(200.0)
        state.update_btc_chainlink(201.0, 2000)
        # Second freeze must not overwrite
        second = state.freeze_resolution_refs(42)
        self.assertEqual(second["beat"], 100.0)
        self.assertEqual(second["chainlink"], 101.0)

    def test_get_resolution_refs_no_history_copy(self):
        state = BotState()
        state.set_price_to_beat(1.0)
        state.update_btc_price(2.0, 1000)
        refs = state.get_resolution_refs()
        self.assertEqual(set(refs.keys()), {"beat", "chainlink", "spot"})
        self.assertNotIn("price_history", refs)


class SizingTests(unittest.TestCase):
    def test_compute_stake_paper(self):
        self.assertAlmostEqual(compute_stake(0.5, 100.0, 80.0), 50.0)
        self.assertAlmostEqual(compute_stake(0.5, 100.0, 40.0), 40.0)
        self.assertEqual(compute_stake(0.5, 100.0, 0.005), 0.0)

    def test_compute_stake_live_caps_wallet(self):
        stake = compute_stake(
            0.5, 100.0, 100.0, wallet_balance=12.0, is_live=True
        )
        self.assertAlmostEqual(stake, 12.0)
        # Paper ignores wallet
        stake_p = compute_stake(
            0.5, 100.0, 100.0, wallet_balance=12.0, is_live=False
        )
        self.assertAlmostEqual(stake_p, 50.0)

    def test_partial_fill_math(self):
        self.assertAlmostEqual(partial_fill_stake(10.0, 0.70), 7.0)
        self.assertEqual(partial_fill_stake(0.0, 0.70), 0.0)
        stake, shares = scale_pending_for_fill(50.0, 100.0, 40.0, 0.50)
        self.assertAlmostEqual(shares, 40.0)
        self.assertAlmostEqual(stake, 20.0)


class BankrollSettleTests(unittest.TestCase):
    def test_apply_settle_updates_equity_consistently(self):
        bot = TradingBot(_paper_config())
        pending = PendingTrade(
            slug="btc-updown-5m-1",
            title="t",
            side="up",
            entry_price=0.70,
            stake=10.0,
            size_label="small",
            risk_pct=10.0,
            reason="test",
            entry_ts=time.time(),
            start_ts=1,
            mode="paper",
            token_id="tok",
        )
        # Win: pnl = 10/0.7 - 10
        expected_pnl = _settle_pnl(10.0, 0.70, True)
        bot._apply_settle_result(
            _SettleResult(
                slug=pending.slug,
                title="t",
                pending=pending,
                winner="up",
                source="test",
                filled_shares=0.0,
            )
        )
        self.assertAlmostEqual(bot._equity, 100.0 + expected_pnl, places=4)

        # Loss
        pending2 = PendingTrade(
            slug="btc-updown-5m-2",
            title="t",
            side="up",
            entry_price=0.70,
            stake=10.0,
            size_label="small",
            risk_pct=10.0,
            reason="test",
            entry_ts=time.time(),
            start_ts=2,
            mode="paper",
            token_id="tok",
        )
        bot._apply_settle_result(
            _SettleResult(
                slug=pending2.slug,
                title="t",
                pending=pending2,
                winner="down",
                source="test",
                filled_shares=0.0,
            )
        )
        self.assertAlmostEqual(
            bot._equity, 100.0 + expected_pnl - 10.0, places=4
        )

    def test_drain_settle_before_stake_sees_pnl(self):
        bot = TradingBot(_paper_config())
        pending = PendingTrade(
            slug="s",
            title="t",
            side="down",
            entry_price=0.70,
            stake=20.0,
            size_label="small",
            risk_pct=20.0,
            reason="x",
            entry_ts=time.time(),
            start_ts=1,
            mode="paper",
            token_id="tok",
        )
        bot._settle_results.put(
            _SettleResult(
                slug="s",
                title="t",
                pending=pending,
                winner="down",
                source="ticks",
                filled_shares=0.0,
            )
        )
        bot._drain_settle_results()
        with bot._bankroll_lock:
            equity = bot._equity
        # Win at 0.70 with 20 stake → +20*(1/0.7 - 1)
        self.assertAlmostEqual(equity, 100.0 + _settle_pnl(20.0, 0.70, True), places=4)
        stake = compute_stake(0.5, 100.0, equity)
        self.assertAlmostEqual(stake, 50.0)  # capped by initial*risk, equity higher

    def test_live_partial_fill_scales_stake(self):
        bot = TradingBot(_paper_config(trading_mode="live"))
        pending = PendingTrade(
            slug="s",
            title="t",
            side="up",
            entry_price=0.50,
            stake=50.0,
            size_label="big",
            risk_pct=50.0,
            reason="x",
            entry_ts=time.time(),
            start_ts=1,
            mode="live",
            token_id="tok",
            requested_shares=100.0,
            filled_shares=0.0,
        )
        bot._apply_settle_result(
            _SettleResult(
                slug="s",
                title="t",
                pending=pending,
                winner="up",
                source="gamma",
                filled_shares=40.0,  # only 40 of 100 shares
            )
        )
        # Live does not mutate paper equity; stake on pending is scaled
        self.assertAlmostEqual(pending.stake, 20.0)
        self.assertAlmostEqual(pending.filled_shares, 40.0)
        self.assertAlmostEqual(bot._equity, 100.0)

    def test_concurrent_settle_applies_all_pnls(self):
        bot = TradingBot(_paper_config())
        pnls = []
        for i, won in enumerate([True, False, True]):
            p = PendingTrade(
                slug=f"s{i}",
                title="t",
                side="up",
                entry_price=0.50,
                stake=10.0,
                size_label="s",
                risk_pct=10.0,
                reason="x",
                entry_ts=time.time(),
                start_ts=i,
                mode="paper",
                token_id="tok",
            )
            pnls.append(_settle_pnl(10.0, 0.50, won))
            bot._settle_results.put(
                _SettleResult(
                    slug=f"s{i}",
                    title="t",
                    pending=p,
                    winner="up" if won else "down",
                    source="t",
                    filled_shares=0.0,
                )
            )

        barriers = []

        def drain():
            bot._drain_settle_results()

        threads = [threading.Thread(target=drain) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertAlmostEqual(bot._equity, 100.0 + sum(pnls), places=4)


class KlineCacheAndFeaturesTests(unittest.TestCase):
    def test_failed_fetch_not_cached(self, tmp_path=None):
        cache_dir = Path(self.id().replace(".", "_") + "_cache")
        # Use a temp dir under system temp via unittest isolation
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            with mock.patch.object(btc_features, "CACHE_DIR", tdir):
                with mock.patch.object(btc_features.requests, "get") as get:
                    get.side_effect = btc_features.requests.RequestException("down")
                    rows = btc_features.fetch_klines(1_700_000_000, 1_700_000_600, use_cache=True)
                    self.assertEqual(rows, [])
                    # No cache files written for failed fetch
                    self.assertEqual(list(tdir.glob("*.json")), [])

    def test_empty_result_not_cached_on_success_http(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            with mock.patch.object(btc_features, "CACHE_DIR", tdir):
                resp = mock.Mock()
                resp.raise_for_status = mock.Mock()
                resp.json.return_value = []
                with mock.patch.object(btc_features.requests, "get", return_value=resp):
                    rows = btc_features.fetch_klines(1_700_000_000, 1_700_000_120, use_cache=True)
                    self.assertEqual(rows, [])
                    self.assertEqual(list(tdir.glob("*.json")), [])

    def test_successful_fetch_is_cached(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            # one closed 1m bar inside range
            start_ms = 1_700_000_000_000
            batch = [[
                start_ms,
                "100", "101", "99", "100.5", "1.0",
                0, 0, 0, 0, 0, 0,
            ]]
            resp = mock.Mock()
            resp.raise_for_status = mock.Mock()
            resp.json.return_value = batch
            with mock.patch.object(btc_features, "CACHE_DIR", tdir):
                with mock.patch.object(btc_features.requests, "get", return_value=resp):
                    rows = btc_features.fetch_klines(
                        1_700_000_000, 1_700_000_060, use_cache=True
                    )
                    self.assertEqual(len(rows), 1)
                    files = list(tdir.glob("*.json"))
                    self.assertEqual(len(files), 1)
                    # Second call hits cache (no network)
                    with mock.patch.object(
                        btc_features.requests, "get", side_effect=AssertionError("no net")
                    ):
                        rows2 = btc_features.fetch_klines(
                            1_700_000_000, 1_700_000_060, use_cache=True
                        )
                        self.assertEqual(len(rows2), 1)

    def test_window_features_sorted_path_correct(self):
        # Build 62 closed bars with known return on last closed bar
        bars = []
        for i in range(62):
            # close grows slowly
            c = 100.0 + i * 0.1
            bars.append((i * 60_000, c - 0.05, c + 0.05, c - 0.05, c, 1.0))
        # entry at bar 62 open → last closed open is 61*60k
        entry_ts = 62 * 60
        feats = window_features(bars, entry_ts, lookback=60)
        self.assertIsNotNone(feats)
        assert feats is not None
        self.assertEqual(feats.shape, (60, 3))
        # Last feature log return ≈ log((100+6.1)/(100+6.0))
        expected_lr = float(np.log((100.0 + 6.1) / (100.0 + 6.0)))
        self.assertAlmostEqual(float(feats[-1, 0]), expected_lr, places=5)

        # Unsorted input still works
        shuffled = list(reversed(bars))
        feats2 = window_features(shuffled, entry_ts, lookback=60)
        self.assertIsNotNone(feats2)
        np.testing.assert_allclose(feats, feats2, rtol=1e-5)

    def test_window_excludes_open_bar(self):
        bars = [(i * 60_000, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(62)]
        bars.append((62 * 60_000, 1.0, 100.0, 0.01, 100.0, 1.0))
        features = window_features(bars, 62 * 60, lookback=60)
        self.assertIsNotNone(features)
        self.assertEqual(float(features[-1, 0]), 0.0)

    def test_unsupported_asset_raises(self):
        with self.assertRaises(ValueError):
            btc_features.fetch_klines(1, 2, asset="doge")


class BacktestCacheSingleLoadTests(unittest.TestCase):
    def test_fetch_candles_loads_cache_once(self):
        from src.backtest import data as bt_data
        from src.backtest.types import CandleDataset

        fake = CandleDataset(
            slug="btc-updown-5m-1",
            title="t",
            start_ts=100,
            end_ts=400,
            winner="up",
            up_token_id="u",
            down_token_id="d",
            ticks=[(100, 0.5, 0.5), (200, 0.7, 0.3)],
        )
        load_calls = {"n": 0}

        def load(asset, start_ts):
            load_calls["n"] += 1
            return fake

        with mock.patch.object(bt_data, "list_candidate_starts", return_value=[100, 200]):
            with mock.patch.object(bt_data, "_load_cache", side_effect=load):
                with mock.patch.object(bt_data, "_build_candle") as build:
                    out = bt_data.fetch_candles("btc", count=2, use_cache=True)
                    self.assertEqual(len(out), 2)
                    # One load per start_ts, not two
                    self.assertEqual(load_calls["n"], 2)
                    build.assert_not_called()

    def test_run_result_reports_actual_data_window(self):
        from src.backtest_server import BacktestRequest, _run_backtest
        from src.backtest.types import CandleDataset

        candles = [
            CandleDataset("first", "first", 100, 400, "up", "u", "d", [(100, .5, .5), (400, .7, .3)]),
            CandleDataset("last", "last", 700, 1000, "down", "u", "d", [(700, .5, .5), (1000, .3, .7)]),
        ]
        result = mock.Mock()
        result.to_dict.return_value = {"candles_loaded": 2}

        with mock.patch("src.backtest_server.get_strategy"), \
             mock.patch("src.backtest_server.fetch_candles", return_value=candles), \
             mock.patch("src.backtest_server.run_backtest", return_value=result):
            response = _run_backtest(BacktestRequest(strategy_id="test"))

        self.assertEqual(response["data_start_ts"], 100)
        self.assertEqual(response["data_end_ts"], 1000)


class ConsecutiveWinStreakTests(unittest.TestCase):
    def test_backtest_engine_passes_exact_consecutive_win_streak(self):
        from strategies.base import Strategy, StrategyMeta, TradeSignal
        from src.backtest.engine import run_backtest
        from src.backtest.types import CandleDataset

        class WinningStrategy(Strategy):
            meta = StrategyMeta(id="test", name="test", description="test")

            def __init__(self):
                super().__init__()
                self.streaks: list[int] = []

            def on_account_update(self, *args, **kwargs):
                super().on_account_update(*args, **kwargs)
                self.streaks.append(self._wins_streak)

            def evaluate(self, _tick, candle, *, entered):
                if entered:
                    return None
                return TradeSignal(side=candle.winner, price=0.5, reason="test")

            def position_risk_fraction(self, *_args):
                return 0.01

        candles = [
            CandleDataset(
                slug=f"c{i}", title=f"c{i}", start_ts=i * 300,
                end_ts=(i + 1) * 300, winner="up", up_token_id="u",
                down_token_id="d", ticks=[(i * 300, 0.5, 0.5)],
            )
            for i in range(6)
        ]
        strategy = WinningStrategy()
        run_backtest(strategy, candles)

        self.assertEqual(strategy.streaks, [0, 1, 2, 3, 4, 5])

    def test_signull_uses_25_percent_after_three_or_four_wins(self):
        from strategies.base import CandleContext, TickContext, TradeSignal
        from strategies.signull_1_0 import Signull10Strategy

        strategy = Signull10Strategy()
        strategy._recent_trustworthy = lambda _candle: (False, 1, 3)  # type: ignore[method-assign]
        strategy._btc_aligned = lambda _ts, _side: (False, 0.5)  # type: ignore[method-assign]
        signal = TradeSignal(side="up", price=0.70, reason="test")
        tick = TickContext(t=1, up=0.70, down=0.30, seconds_into_candle=1, seconds_to_close=299)
        candle = CandleContext(slug="test", title="test", start_ts=0, end_ts=300, winner="up")
        for streak in (3, 4):
            strategy._wins_streak = streak
            self.assertEqual(strategy.position_risk_fraction(signal, tick, candle), 0.25)
            self.assertEqual(strategy.size_label(0.25), "hot-streak")


class Signull11KellyTests(unittest.TestCase):
    def test_closed_candle_hook_is_a_clean_no_op(self):
        from strategies.signull_1_1 import Signull11Strategy

        strategy = Signull11Strategy()

        self.assertFalse(
            strategy.register_closed_candle(
                "btc-updown-123", [(1, 0.70, 0.30)]
            )
        )

    def test_uses_ten_percent_current_bankroll_during_calibration(self):
        from strategies.base import CandleContext, TickContext, TradeSignal
        from strategies.signull_1_1 import Signull11Strategy

        strategy = Signull11Strategy()
        strategy.on_account_update(120.0, 100.0, 120.0)
        signal = TradeSignal(side="up", price=0.70, reason="test")
        tick = TickContext(t=1, up=0.70, down=0.30, seconds_into_candle=10, seconds_to_close=290)
        candle = CandleContext(slug="test", title="test", start_ts=0, end_ts=300, winner="up")

        # The engine takes a fraction of initial capital; 12% of initial is
        # exactly a 10% allocation of the current $120 bankroll.
        self.assertAlmostEqual(strategy.position_risk_fraction(signal, tick, candle), 0.12)
        self.assertEqual(strategy.size_label(0.12), "explore")

    def test_moves_to_conservative_kelly_after_warmup(self):
        from strategies.base import CandleContext, TickContext, TradeSignal
        from strategies.signull_1_1 import Signull11Strategy

        strategy = Signull11Strategy()
        strategy.on_account_update(100.0, 100.0, 100.0)
        strategy._stats["all"] = [30, 30]
        signal = TradeSignal(side="up", price=0.70, reason="test")
        tick = TickContext(t=1, up=0.70, down=0.30, seconds_into_candle=10, seconds_to_close=290)
        candle = CandleContext(slug="test", title="test", start_ts=0, end_ts=300, winner="up")

        risk = strategy.position_risk_fraction(signal, tick, candle)
        self.assertGreater(risk, 0.0)
        self.assertLessEqual(risk, 0.10)
        self.assertEqual(strategy.size_label(risk), "kelly")

    def test_declined_backtest_signal_keeps_calibrating(self):
        """A no-edge decision must not freeze Signull 1.1's sample forever."""
        from strategies.base import CandleContext, TickContext, TradeSignal
        from strategies.signull_1_1 import Signull11Strategy

        strategy = Signull11Strategy()
        strategy.on_account_update(100.0, 100.0, 100.0)
        strategy._stats["all"] = [0, 30]
        signal = TradeSignal(side="up", price=0.70, reason="test")
        tick = TickContext(t=1, up=0.70, down=0.30, seconds_into_candle=10, seconds_to_close=290)
        candle = CandleContext(slug="test", title="test", start_ts=0, end_ts=300, winner="up")

        self.assertEqual(strategy.position_risk_fraction(signal, tick, candle), 0.0)
        strategy.on_signal_resolved(True, traded=False)
        self.assertEqual(strategy._stats["all"], [1, 31])


class DeclinedSignalBacktestTests(unittest.TestCase):
    def test_engine_reports_outcome_for_zero_risk_signal(self):
        from strategies.base import Strategy, StrategyMeta, TradeSignal
        from src.backtest.engine import run_backtest
        from src.backtest.types import CandleDataset

        class ObserveStrategy(Strategy):
            meta = StrategyMeta(id="observe", name="observe", description="observe")

            def __init__(self):
                super().__init__()
                self.outcomes = []

            def evaluate(self, _tick, candle, *, entered):
                return None if entered else TradeSignal(candle.winner, 0.5, "observe")

            def position_risk_fraction(self, *_args):
                return 0.0

            def on_signal_resolved(self, won, *, traded):
                self.outcomes.append((won, traded))

        candle = CandleDataset("c", "c", 0, 300, "up", "u", "d", [(0, .5, .5)])
        strategy = ObserveStrategy()
        result = run_backtest(strategy, [candle])
        self.assertEqual(result.candles_traded, 0)
        self.assertEqual(strategy.outcomes, [(True, False)])


class AssetFeedTests(unittest.TestCase):
    def test_btc_feed_rejects_unknown_asset(self):
        from src.btc_feed import BtcPriceFeed

        with self.assertRaises(ValueError):
            BtcPriceFeed(BotState(), asset="doge")

    def test_btc_feed_symbol_map(self):
        from src.btc_feed import BtcPriceFeed, BINANCE_SYMBOLS, CHAINLINK_SYMBOLS

        for asset in ("btc", "eth", "sol", "xrp"):
            feed = BtcPriceFeed(BotState(), asset=asset)
            self.assertEqual(feed._binance_sym, BINANCE_SYMBOLS[asset])
            self.assertEqual(feed._chainlink_sym, CHAINLINK_SYMBOLS[asset])


class ScheduleKlinesNonBlockingTests(unittest.TestCase):
    def test_schedule_returns_immediately(self):
        from strategies.signull_1_0 import Signull10Strategy

        strat = Signull10Strategy()
        started = time.perf_counter()
        entered = threading.Event()
        release = threading.Event()

        def slow_fetch(around_ts=None):
            entered.set()
            release.wait(timeout=2.0)
            return [(i * 60_000, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(70)]

        with mock.patch.object(strat, "_fetch_klines_rows", side_effect=slow_fetch):
            strat._klines = None
            strat._klines_fetched_at = 0.0
            strat._klines_refreshing = False
            strat.schedule_klines_refresh(int(time.time()))
            elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.2)
        self.assertTrue(entered.wait(timeout=1.0))
        release.set()
        time.sleep(0.05)

    def test_empty_fetch_does_not_block_retries(self):
        from strategies.signull_1_0 import Signull10Strategy

        strat = Signull10Strategy()
        calls = {"n": 0}

        def empty_fetch(*_a, **_k):
            calls["n"] += 1
            return []

        with mock.patch(
            "strategies.signull_1_0.fetch_klines", side_effect=empty_fetch
        ):
            strat.refresh_btc_klines(1_700_000_000)
            self.assertIsNone(strat._klines)
            self.assertEqual(strat._klines_fetched_at, 0.0)
            self.assertFalse(strat._klines_refreshing)
            # Immediate second call must hit network again (not 45s empty cache)
            strat.refresh_btc_klines(1_700_000_000)
            self.assertEqual(calls["n"], 2)

    def test_empty_list_not_treated_as_fresh_cache(self):
        from strategies.signull_1_0 import Signull10Strategy

        strat = Signull10Strategy()
        # Poison state that the old bug would create
        strat._klines = []
        strat._klines_fetched_at = time.time()
        calls = {"n": 0}

        def one_bar(*_a, **_k):
            calls["n"] += 1
            return [(0, 1.0, 1.0, 1.0, 1.0, 1.0)]

        with mock.patch(
            "strategies.signull_1_0.fetch_klines", side_effect=one_bar
        ):
            strat.refresh_btc_klines(1_700_000_000)
            self.assertEqual(calls["n"], 1)
            self.assertEqual(len(strat._klines or []), 1)

    def test_schedule_single_in_flight(self):
        from strategies.signull_1_0 import Signull10Strategy

        strat = Signull10Strategy()
        started = []
        barrier = threading.Event()
        release = threading.Event()

        def blocking_fetch(around_ts=None):
            started.append(around_ts)
            barrier.set()
            release.wait(timeout=2.0)
            return [(i * 60_000, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(70)]

        with mock.patch.object(strat, "_fetch_klines_rows", side_effect=blocking_fetch):
            strat._klines = None
            strat._klines_fetched_at = 0.0
            strat._klines_refreshing = False
            strat.schedule_klines_refresh(1)
            self.assertTrue(barrier.wait(timeout=1.0))
            # While first is in flight, further schedules must not spawn workers
            strat.schedule_klines_refresh(2)
            strat.schedule_klines_refresh(3)
            time.sleep(0.05)
            self.assertEqual(len(started), 1)
            release.set()
            time.sleep(0.1)
            self.assertFalse(strat._klines_refreshing)
            self.assertTrue(strat._klines)

    def test_window_features_none_clears_and_reschedules(self):
        from strategies.signull_1_0 import Signull10Strategy

        strat = Signull10Strategy()
        # Too few bars → window_features returns None
        strat._klines = [(i * 60_000, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(5)]
        strat._klines_fetched_at = time.time()
        scheduled = []

        def capture(around_ts=None):
            scheduled.append(around_ts)

        with mock.patch.object(strat, "schedule_klines_refresh", side_effect=capture):
            ok, align = strat._btc_aligned(1000, "up")
            self.assertFalse(ok)
            self.assertEqual(align, 0.5)
            self.assertIsNone(strat._klines)
            self.assertEqual(strat._klines_fetched_at, 0.0)
            self.assertEqual(scheduled, [1000])


if __name__ == "__main__":
    unittest.main()
