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
from src.sizing import (
    cap_stake_for_taker_fee,
    compute_stake,
    estimate_taker_fee,
    partial_fill_stake,
    scale_pending_for_fill,
)
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
        strategy_risk_pct=0.10,
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

    def test_taker_fee_and_cash_cap(self):
        # 100 shares at 70¢, with the crypto 7% fee coefficient, costs
        # 70 * 0.07 * 0.30 = $1.47 in taker fees.
        self.assertAlmostEqual(estimate_taker_fee(70.0, 0.70, 0.07), 1.47)
        capped = cap_stake_for_taker_fee(100.0, 0.70, 0.07, 100.0)
        self.assertAlmostEqual(capped + estimate_taker_fee(capped, 0.70, 0.07), 100.0)


def _clear_paper_session() -> None:
    from src.session_store import session_path

    path = session_path(_paper_config())
    if path.exists():
        path.unlink()


class BankrollSettleTests(unittest.TestCase):
    def setUp(self):
        _clear_paper_session()
        self._save_patcher = mock.patch("src.bot.save_session")
        self._save_patcher.start()

    def tearDown(self):
        self._save_patcher.stop()
        _clear_paper_session()

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


class Signull10ExecutionTests(unittest.TestCase):
    def test_requires_a_real_upward_cross_and_uses_observed_price(self):
        from strategies.base import CandleContext, TickContext
        from strategies.signull_1_0 import Signull10Strategy

        strategy = Signull10Strategy()
        candle = CandleContext("c", "c", 0, 300, "")
        first = TickContext(1, 0.50, 0.50, 1, 299)
        below = TickContext(2, 0.69, 0.31, 2, 298)
        crossed = TickContext(3, 0.73, 0.27, 3, 297)

        self.assertIsNone(strategy.evaluate(first, candle, entered=False))
        self.assertIsNone(strategy.evaluate(below, candle, entered=False))
        signal = strategy.evaluate(crossed, candle, entered=False)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "up")
        self.assertAlmostEqual(signal.price, 0.73)
        self.assertAlmostEqual(signal.taker_fee_rate, 0.07)

    def test_does_not_enter_from_an_already_above_threshold_first_quote(self):
        from strategies.base import CandleContext, TickContext
        from strategies.signull_1_0 import Signull10Strategy

        strategy = Signull10Strategy()
        candle = CandleContext("c", "c", 0, 300, "")
        self.assertIsNone(
            strategy.evaluate(TickContext(1, 0.75, 0.25, 1, 299), candle, entered=False)
        )
        self.assertIsNone(
            strategy.evaluate(TickContext(2, 0.80, 0.20, 2, 298), candle, entered=False)
        )

    def test_backtest_debits_signull_taker_fee(self):
        from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal
        from src.backtest.engine import run_backtest
        from src.backtest.types import CandleDataset

        class FeeStrategy(Strategy):
            meta = StrategyMeta(id="fee", name="fee", description="fee")

            def evaluate(self, _tick, _candle, *, entered):
                if entered:
                    return None
                return TradeSignal("up", 0.70, "fee", taker_fee_rate=0.07)

            def position_risk_fraction(self, *_args):
                return 0.10

        candle = CandleDataset(
            "c", "c", 0, 300, "up", "u", "d", [(0, 0.50, 0.50)]
        )
        result = run_backtest(FeeStrategy(), [candle])

        self.assertAlmostEqual(result.trades[0].stake, 10.0)
        self.assertAlmostEqual(result.trades[0].entry_fee, 0.21)
        # Backtest summary rounds ending capital to cents; the trade record
        # retains the exact fee amount used to reach it.
        self.assertAlmostEqual(result.ending_capital, 104.08, places=2)


class Signull12RegimeKellyTests(unittest.TestCase):
    def test_enters_on_the_first_threshold_eligible_observation(self):
        from strategies.base import CandleContext, TickContext
        from strategies.signull_1_2 import Signull12Strategy

        strategy = Signull12Strategy()
        candle = CandleContext("c", "c", 0, 300, "")
        self.assertIsNone(
            strategy.evaluate(TickContext(1, .50, .50, 1, 299), candle, entered=False)
        )
        self.assertIsNone(
            strategy.evaluate(TickContext(2, .69, .31, 2, 298), candle, entered=False)
        )
        signal = strategy.evaluate(
            TickContext(3, .72, .28, 3, 297), candle, entered=False
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "up")
        self.assertAlmostEqual(signal.price, .72)
        self.assertAlmostEqual(signal.taker_fee_rate, .07)

    def test_enters_when_the_first_recorded_quote_is_already_eligible(self):
        from strategies.base import CandleContext, TickContext
        from strategies.signull_1_2 import Signull12Strategy

        strategy = Signull12Strategy()
        candle = CandleContext("c", "c", 0, 300, "")
        signal = strategy.evaluate(
            TickContext(1, .76, .24, 1, 299), candle, entered=False
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "up")
        self.assertAlmostEqual(signal.price, .76)

    def test_exploration_is_small_fraction_of_current_bankroll(self):
        from strategies.base import CandleContext, TickContext, TradeSignal
        from strategies.signull_1_2 import Signull12Strategy

        strategy = Signull12Strategy()
        strategy.on_account_update(120.0, 100.0, 120.0)
        signal = TradeSignal("up", .70, "test", taker_fee_rate=.07)
        tick = TickContext(10, .70, .30, 10, 290)
        candle = CandleContext("c", "c", 0, 300, "")

        # 2% of current $120 is $2.40, represented as 2.4% of initial $100.
        self.assertAlmostEqual(
            strategy.position_risk_fraction(signal, tick, candle), .024
        )
        self.assertEqual(strategy.size_label(.024), "explore")

    def test_no_edge_is_skipped_but_still_updates_calibration(self):
        from strategies.base import CandleContext, TickContext, TradeSignal
        from strategies.signull_1_2 import Signull12Strategy

        strategy = Signull12Strategy()
        strategy._stats["all"] = [71, 100]
        signal = TradeSignal("up", .70, "test", taker_fee_rate=.07)
        tick = TickContext(10, .70, .30, 10, 290)
        candle = CandleContext("c", "c", 0, 300, "")

        self.assertEqual(strategy.position_risk_fraction(signal, tick, candle), 0.0)
        self.assertEqual(strategy.size_label(0.0), "no-edge")
        strategy.on_signal_resolved(True, traded=False)
        self.assertEqual(strategy._stats["all"], [72, 101])

    def test_positive_edge_uses_capped_fractional_kelly_not_win_streak(self):
        from strategies.base import CandleContext, TickContext, TradeSignal
        from strategies.signull_1_2 import Signull12Strategy

        strategy = Signull12Strategy()
        strategy._stats["all"] = [100, 100]
        strategy._wins_streak = 12
        signal = TradeSignal("up", .70, "test", taker_fee_rate=.07)
        tick = TickContext(10, .70, .30, 10, 290)
        candle = CandleContext("c", "c", 0, 300, "")

        risk = strategy.position_risk_fraction(signal, tick, candle)
        self.assertGreater(risk, 0.0)
        self.assertLessEqual(risk, .10)
        self.assertEqual(strategy.size_label(risk), "kelly")


class Signull11AlwaysInTests(unittest.TestCase):
    def test_closed_candle_hook_is_a_clean_no_op(self):
        from strategies.signull_1_1 import Signull11Strategy

        strategy = Signull11Strategy()

        self.assertFalse(
            strategy.register_closed_candle(
                "btc-updown-123", [(1, 0.70, 0.30)]
            )
        )

    def test_enters_at_threshold_when_favorite_crosses(self):
        from strategies.base import CandleContext, TickContext
        from strategies.signull_1_1 import Signull11Strategy

        strategy = Signull11Strategy()
        candle = CandleContext("c", "c", 0, 300, "up")
        signal = strategy.evaluate(
            TickContext(1, 0.70, 0.30, 10, 290), candle, entered=False
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "up")
        self.assertAlmostEqual(signal.price, 0.70)

    def test_enters_near_close_when_threshold_never_hits(self):
        from strategies.base import CandleContext, TickContext
        from strategies.signull_1_1 import Signull11Strategy

        strategy = Signull11Strategy()
        candle = CandleContext("c", "c", 0, 300, "up")
        self.assertIsNone(
            strategy.evaluate(
                TickContext(1, 0.55, 0.45, 10, 290), candle, entered=False
            )
        )
        signal = strategy.evaluate(
            TickContext(2, 0.58, 0.42, 298, 2), candle, entered=False
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "up")
        self.assertAlmostEqual(signal.price, 0.58)

    def test_uses_fixed_fraction_of_current_bankroll(self):
        from strategies.base import CandleContext, TickContext, TradeSignal
        from strategies.signull_1_1 import Signull11Strategy

        strategy = Signull11Strategy()
        strategy.on_account_update(120.0, 100.0, 120.0)
        signal = TradeSignal(side="up", price=0.70, reason="test")
        tick = TickContext(t=1, up=0.70, down=0.30, seconds_into_candle=10, seconds_to_close=290)
        candle = CandleContext(slug="test", title="test", start_ts=0, end_ts=300, winner="up")

        # 10% of current $120 is $12, represented as 12% of initial $100.
        self.assertAlmostEqual(strategy.position_risk_fraction(signal, tick, candle), 0.12)
        self.assertEqual(strategy.size_label(0.12), "flat")

    def test_never_declines_a_signal_for_zero_risk(self):
        from strategies.base import CandleContext, TickContext, TradeSignal
        from strategies.signull_1_1 import Signull11Strategy

        strategy = Signull11Strategy()
        strategy.on_account_update(100.0, 100.0, 100.0)
        signal = TradeSignal(side="up", price=0.70, reason="test")
        tick = TickContext(t=1, up=0.70, down=0.30, seconds_into_candle=10, seconds_to_close=290)
        candle = CandleContext(slug="test", title="test", start_ts=0, end_ts=300, winner="up")

        risk = strategy.position_risk_fraction(signal, tick, candle)
        self.assertGreater(risk, 0.0)
        self.assertEqual(strategy.size_label(risk), "flat")


class FastSettlementTests(unittest.TestCase):
    def setUp(self):
        _clear_paper_session()
        self._save_patcher = mock.patch("src.bot.save_session")
        self._save_patcher.start()

    def tearDown(self):
        self._save_patcher.stop()
        _clear_paper_session()

    def test_winner_from_price_refs_is_instant(self):
        from src.markets import winner_from_price_refs

        winner, source = winner_from_price_refs(
            {"beat": 100.0, "chainlink": 100.5, "spot": 100.4}
        )
        self.assertEqual(winner, "up")
        self.assertIn("chainlink", source)

    def test_resolve_winner_prefers_frozen_oracle_over_gamma(self):
        from src.bot import TradingBot

        bot = TradingBot(_paper_config())
        with mock.patch("src.bot.resolve_candle_winner", return_value=None) as gamma:
            with mock.patch("src.bot.time.sleep") as sleep:
                winner, source = bot._resolve_winner_reliable(
                    start_ts=1,
                    ticks=[(1, 0.55, 0.45)],
                    refs={"beat": 100.0, "chainlink": 99.5, "spot": 99.6},
                )
        self.assertEqual(winner, "down")
        self.assertIn("btc", source)
        gamma.assert_not_called()
        sleep.assert_not_called()

    def test_apply_settle_updates_equity_history_immediately(self):
        from src.bot import TradingBot

        bot = TradingBot(_paper_config())
        pending = PendingTrade(
            slug="s",
            title="t",
            side="up",
            entry_price=0.70,
            stake=10.0,
            size_label="flat",
            risk_pct=10.0,
            reason="x",
            entry_ts=time.time(),
            start_ts=1,
            mode="paper",
            token_id="tok",
        )
        bot._apply_settle_result(
            _SettleResult(
                slug="s",
                title="t",
                pending=pending,
                winner="up",
                source="btc",
                filled_shares=0.0,
            )
        )
        snap = bot.state.get_snapshot()
        self.assertTrue(snap["equity_history"])
        self.assertAlmostEqual(snap["equity_history"][-1]["v"], bot._equity, places=4)
        self.assertIsNone(bot._pending)


class SessionPersistenceTests(unittest.TestCase):
    def test_bot_restores_paper_bankroll_from_session(self):
        from src.session_store import load_session, save_session, session_path

        config = _paper_config(strategy_id="signull_1_1")
        path = session_path(config)
        if path.exists():
            path.unlink()

        save_session(
            config,
            {
                "initial_capital": 100.0,
                "equity": 118.5,
                "peak_equity": 120.0,
                "wins_recent": [True, False],
                "wins_streak": 0,
                "losses_streak": 1,
                "trades_placed": 4,
                "strategy_trades": [{"slug": "btc-1", "won": True}],
                "equity_history": [{"t": 1.0, "v": 118.5, "mode": "paper"}],
            },
        )

        bot = TradingBot(config, BotState(), session=load_session(config))
        self.assertAlmostEqual(bot._equity, 118.5)
        self.assertAlmostEqual(bot._peak, 120.0)
        self.assertEqual(bot._wins_recent, [True, False])
        self.assertEqual(bot.state.get_snapshot()["strategy_trades"][0]["slug"], "btc-1")

        if path.exists():
            path.unlink()

    def test_persist_session_writes_round_trip(self):
        from src.session_store import load_session, session_path

        config = _paper_config(strategy_id="signull_1_1")
        path = session_path(config)
        if path.exists():
            path.unlink()

        bot = TradingBot(config, BotState())
        bot._equity = 112.0
        bot._peak = 115.0
        bot.state.record_strategy_trade({"slug": "btc-2", "won": False})
        bot.persist_session()

        restored = load_session(config)
        assert restored is not None
        self.assertAlmostEqual(restored["equity"], 112.0)
        self.assertEqual(restored["strategy_trades"][0]["slug"], "btc-2")

        if path.exists():
            path.unlink()


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

    def test_binance_feed_rejects_unlabeled_ticks(self):
        from src.btc_feed import BtcPriceFeed

        feed = BtcPriceFeed(BotState(), asset="btc")
        self.assertFalse(feed._binance_point_matches({"value": 100.0}))
        self.assertTrue(
            feed._binance_point_matches({"symbol": "btcusdt", "value": 100.0})
        )
        self.assertFalse(
            feed._binance_point_matches({"symbol": "ethusdt", "value": 100.0})
        )


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
