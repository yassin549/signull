"""Candle rollover: identity is start_ts, feed keeps the socket."""

from __future__ import annotations

import unittest
from unittest import mock

from src.config import BotConfig
from src.feed import MarketFeed
from src.markets import CandleMarket, provisional_market_dict
from src.state import BotState


def _paper_config() -> BotConfig:
    return BotConfig(
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


def _market(start: int, slug: str | None = None) -> CandleMarket:
    from datetime import datetime, timezone

    return CandleMarket(
        slug=slug or f"btc-updown-5m-{start}",
        title="BTC Up or Down 5m",
        end_date=datetime.fromtimestamp(start + 300, tz=timezone.utc),
        condition_id="c",
        up_token_id=f"up-{start}",
        down_token_id=f"down-{start}",
        up_price=0.5,
        down_price=0.5,
        tick_size="0.01",
        accepting_orders=True,
    )


class CandleSeqTests(unittest.TestCase):
    def test_clear_increments_candle_seq(self):
        state = BotState()
        snap0 = state.get_snapshot(history_points=0)
        self.assertEqual(snap0["candle_seq"], 0)
        state.clear_market_data(closing_start_ts=100)
        self.assertEqual(state.get_snapshot(history_points=0)["candle_seq"], 1)

    def test_provisional_slug_matches_real_event(self):
        stub = provisional_market_dict("btc", 1_700_000_000)
        self.assertEqual(stub["slug"], "btc-updown-5m-1700000000")
        self.assertEqual(stub["candle_start_ts"], 1_700_000_000)
        self.assertTrue(stub["provisional"])


class FeedResubscribeTests(unittest.IsolatedAsyncioTestCase):
    async def test_switch_resubscribes_without_closing(self):
        state = BotState()
        feed = MarketFeed(_paper_config(), state)
        first = _market(1_700_000_000)
        nxt = _market(1_700_000_300)
        feed._active_market = first
        feed._active_slug = first.slug

        ws = mock.AsyncMock()
        with mock.patch.object(feed, "_bootstrap_book", new=mock.AsyncMock()):
            await feed._switch_candle(nxt)
            await feed._subscribe(ws, [nxt.up_token_id, nxt.down_token_id])

        ws.close.assert_not_called()
        ws.send.assert_awaited()
        sent = ws.send.await_args.args[0]
        self.assertIn(nxt.up_token_id, sent)
        self.assertEqual(feed._token_map[nxt.up_token_id], "up")
        self.assertEqual(state.get_snapshot(history_points=0)["market"]["slug"], nxt.slug)


class MergeAccountTests(unittest.TestCase):
    def test_merge_keeps_wallet_fields(self):
        state = BotState()
        state.merge_account({"balance_usdc": 42.0, "connected": True})
        state.merge_account({"paper_equity": 100.0, "mode": "paper"})
        acc = state.get_account()
        self.assertEqual(acc["balance_usdc"], 42.0)
        self.assertTrue(acc["connected"])
        self.assertEqual(acc["paper_equity"], 100.0)
        self.assertEqual(acc["mode"], "paper")


if __name__ == "__main__":
    unittest.main()
