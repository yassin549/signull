"""Wallet monitor + account helpers (mocked CLOB / RPC)."""

from __future__ import annotations

import unittest
from unittest import mock

from src.account import (
    WalletCheck,
    gas_payer_address,
    resolve_signature_type,
    signature_type_candidates,
)
from src.config import BotConfig
from src.state import BotState
from src.wallet_monitor import WalletMonitor, _reserved_notional


def _cfg(**overrides) -> BotConfig:
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


class GasPayerTests(unittest.TestCase):
    def test_eoa_uses_signer(self):
        self.assertEqual(
            gas_payer_address(0, "0xsigner", "0xfunder"),
            "0xsigner",
        )

    def test_proxy_uses_funder(self):
        self.assertEqual(
            gas_payer_address(1, "0xsigner", "0xfunder"),
            "0xfunder",
        )

    def test_deposit_wallet_uses_funder(self):
        self.assertEqual(
            gas_payer_address(3, "0xsigner", "0xfunder"),
            "0xfunder",
        )


class ResolveSignatureTypeTests(unittest.TestCase):
    def test_deployed_deposit_wallet_is_type_3(self):
        with mock.patch(
            "src.account.wallet_is_deployed",
            side_effect=lambda _addr, kind: kind == "WALLET",
        ):
            self.assertEqual(resolve_signature_type("0xabc", 1), 3)

    def test_deployed_safe_is_type_2(self):
        with mock.patch(
            "src.account.wallet_is_deployed",
            side_effect=lambda _addr, kind: kind == "SAFE",
        ):
            self.assertEqual(resolve_signature_type("0xabc", 1), 2)

    def test_relayer_down_keeps_configured_type(self):
        with mock.patch("src.account.wallet_is_deployed", return_value=None):
            self.assertEqual(resolve_signature_type("0xabc", 1), 1)

    def test_both_deployed_keeps_configured_type(self):
        with mock.patch("src.account.wallet_is_deployed", return_value=True):
            self.assertEqual(resolve_signature_type("0xabc", 2), 2)
            self.assertEqual(resolve_signature_type("0xabc", 3), 3)

    def test_candidates_try_safe_before_deposit_when_both_deployed(self):
        with mock.patch("src.account.wallet_is_deployed", return_value=True):
            # Configured type first, then the other smart-wallet types.
            self.assertEqual(signature_type_candidates("0xabc", 3)[:3], [3, 2, 1])
            self.assertEqual(signature_type_candidates("0xabc", 2)[:3], [2, 3, 1])


class ReservedNotionalTests(unittest.TestCase):
    def test_remaining_times_price(self):
        orders = [
            {"price": 0.50, "original_size": 20, "size_matched": 4},
            {"price": 0.70, "originalSize": 10, "sizeMatched": 10},
        ]
        self.assertAlmostEqual(_reserved_notional(orders), 8.0)


class WalletMonitorNoKeysTests(unittest.TestCase):
    def test_poll_without_wallet_is_disconnected(self):
        state = BotState()
        mon = WalletMonitor(_cfg(), state)
        mon.poll_once()
        acc = state.get_account()
        self.assertFalse(acc["connected"])
        self.assertFalse(acc["verified"])
        self.assertTrue(acc["tips"])


class WalletMonitorPollTests(unittest.TestCase):
    def test_poll_merges_clob_and_rpc(self):
        cfg = _cfg(
            private_key="0x" + "1" * 64,
            funder_address="0x" + "2" * 40,
        )
        state = BotState()
        mon = WalletMonitor(cfg, state)
        client = mock.Mock()
        client.is_authenticated = True
        client.get_collateral_snapshot.return_value = {
            "balance_usdc": 25.5,
            "allowance_usdc": 100.0,
        }
        client.get_open_orders.return_value = [
            {"price": 0.5, "original_size": 10, "size_matched": 0},
        ]
        mon.client = client

        with mock.patch("src.wallet_monitor.fetch_chain_head", return_value=(12_345, 0.02)), \
             mock.patch("src.wallet_monitor.fetch_pol_balance", return_value=1.25), \
             mock.patch("src.wallet_monitor.fetch_onchain_usdc", return_value=25.0), \
             mock.patch("src.wallet_monitor.fetch_onchain_pusd", return_value=25.5), \
             mock.patch("src.wallet_monitor.fetch_pusd_exchange_allowance", return_value=100.0), \
             mock.patch("src.wallet_monitor.fetch_positions", return_value=[]):
            mon.poll_once()

        acc = state.get_account()
        self.assertTrue(acc["connected"])
        self.assertAlmostEqual(acc["balance_usdc"], 25.5)
        self.assertAlmostEqual(acc["reserved_usdc"], 5.0)
        self.assertAlmostEqual(acc["available_usdc"], 20.5)
        self.assertAlmostEqual(acc["gas_pol"], 1.25)
        self.assertAlmostEqual(acc["onchain_usdc"], 25.0)
        self.assertAlmostEqual(acc["onchain_pusd"], 25.5)
        health = state.get_snapshot(history_points=0)["health"]
        self.assertTrue(health["wallet"]["ok"])
        self.assertTrue(health["rpc"]["ok"])

    def test_poll_uses_onchain_pusd_when_clob_reports_zero(self):
        cfg = _cfg(
            private_key="0x" + "1" * 64,
            funder_address="0x" + "2" * 40,
            trading_mode="live",
        )
        state = BotState()
        mon = WalletMonitor(cfg, state)
        client = mock.Mock()
        client.is_authenticated = True
        client.get_collateral_snapshot.return_value = {
            "balance_usdc": 0.0,
            "allowance_usdc": 0.0,
        }
        client.get_open_orders.return_value = []
        mon.client = client

        with mock.patch("src.wallet_monitor.fetch_chain_head", return_value=(12_345, 0.02)), \
             mock.patch("src.wallet_monitor.fetch_pol_balance", return_value=1.25), \
             mock.patch("src.wallet_monitor.fetch_onchain_usdc", return_value=0.0), \
             mock.patch("src.wallet_monitor.fetch_onchain_pusd", return_value=10.16), \
             mock.patch("src.wallet_monitor.fetch_pusd_exchange_allowance", return_value=500.0), \
             mock.patch("src.wallet_monitor.fetch_positions", return_value=[]):
            mon.poll_once()

        acc = state.get_account()
        self.assertAlmostEqual(acc["balance_usdc"], 10.16)
        self.assertAlmostEqual(acc["available_usdc"], 10.16)
        self.assertAlmostEqual(acc["allowance_usdc"], 500.0)
        self.assertEqual(acc["balance_source"], "onchain_pusd")


class LiveResetApiTests(unittest.TestCase):
    def test_live_mode_cannot_reset_account(self):
        from fastapi.testclient import TestClient
        from src.server import create_app

        cfg = _cfg(trading_mode="live")
        app = create_app(cfg)
        with TestClient(app) as client:
            res = client.post("/api/account/reset")
        self.assertEqual(res.status_code, 403)

    def test_health_endpoint(self):
        from fastapi.testclient import TestClient
        from src.server import create_app

        app = create_app(_cfg())
        with TestClient(app) as client:
            res = client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("uptime_sec", body)
        self.assertIn("feed", body)
        self.assertIn("wallet", body)


class MergeAccountTests(unittest.TestCase):
    def test_null_balance_does_not_wipe_last_good_usdc(self):
        state = BotState()
        state.merge_account({"mode": "live", "balance_usdc": 41.25, "allowance_usdc": 10.0})
        state.merge_account({"mode": "live", "balance_usdc": None, "allowance_usdc": None, "connected": False})
        acc = state.get_account()
        self.assertAlmostEqual(acc["balance_usdc"], 41.25)
        self.assertAlmostEqual(acc["allowance_usdc"], 10.0)
        self.assertFalse(acc["connected"])


class WalletCheckDictTests(unittest.TestCase):
    def test_to_dict_includes_gas_fields(self):
        check = WalletCheck(
            ok=True,
            signer_address="0xabc",
            funder_address="0xdef",
            signature_type=1,
            signature_label="Proxy",
            api_connected=True,
            balance_usdc=10.0,
            issues=[],
            tips=[],
            gas_pol=0.2,
            onchain_usdc=9.5,
        )
        data = check.to_dict()
        self.assertEqual(data["gas_pol"], 0.2)
        self.assertEqual(data["onchain_usdc"], 9.5)


if __name__ == "__main__":
    unittest.main()
