"""Background wallet poller — Predict.fun API + BNB Chain RPC."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from eth_account import Account

from .account import fetch_bnb_balance, fetch_chain_head, fetch_onchain_usdt
from .config import BotConfig
from .predict_client import PredictClient
from .state import BotState

logger = logging.getLogger(__name__)


class WalletMonitor:
    def __init__(self, config: BotConfig, state: BotState):
        self.config = config
        self.state = state
        self.client: PredictClient | None = None
        self._last_orders_at = 0.0
        self._last_rpc_at = 0.0
        self._backoff_until = 0.0
        self._interval = max(0.25, float(config.wallet_poll_ms) / 1000.0)
        self._cached_orders: list[dict] = []
        self._signer: str | None = None
        if config.private_key:
            try:
                key = (
                    config.private_key
                    if config.private_key.startswith("0x")
                    else f"0x{config.private_key}"
                )
                self._signer = Account.from_key(key).address
            except Exception:
                self._signer = None

    def _ensure_client(self) -> PredictClient | None:
        if not self.config.has_wallet:
            return None
        if self.client is not None:
            return self.client
        try:
            self.client = PredictClient(self.config, force_auth=True)
            return self.client
        except Exception as exc:
            logger.warning("Wallet monitor Predict.fun auth failed: %s", exc)
            self._merge_health_wallet(ok=False, error=str(exc))
            return None

    def _merge_health_wallet(self, **fields: Any) -> None:
        snap = self.state.get_snapshot(history_points=0)
        health = dict(snap.get("health") or {})
        wallet = dict(health.get("wallet") or {})
        wallet.update(fields)
        wallet["updated_at"] = time.time()
        health["wallet"] = wallet
        self.state.update(health=health)

    def _merge_health_rpc(self, **fields: Any) -> None:
        snap = self.state.get_snapshot(history_points=0)
        health = dict(snap.get("health") or {})
        rpc = dict(health.get("rpc") or {})
        rpc.update(fields)
        rpc["updated_at"] = time.time()
        health["rpc"] = rpc
        self.state.update(health=health)

    def _identity_patch(self) -> dict[str, Any]:
        return {
            "signer_address": self._signer,
            "funder_address": self.config.funder_address,
            "mode": self.config.trading_mode,
            "has_wallet": self.config.has_wallet,
        }

    def poll_once(self) -> None:
        now = time.time()
        if now < self._backoff_until:
            return

        patch: dict[str, Any] = {
            **self._identity_patch(),
            "updated_at": now,
        }

        if not self.config.has_wallet:
            patch.update({
                "connected": False,
                "verified": False,
                "_stale_threshold_sec": self.config.wallet_balance_stale_sec,
            })
            self.state.merge_account(patch)
            self._merge_health_wallet(ok=False, error="no wallet configured")
            return

        client = self._ensure_client()
        connected = bool(client and client.is_authenticated)
        patch["connected"] = connected
        patch["verified"] = connected
        patch["_stale_threshold_sec"] = self.config.wallet_balance_stale_sec

        if connected and client is not None:
            try:
                bal = client.get_balance_usdt()
                if bal is not None:
                    patch["balance_usdt"] = bal
                    patch["balance_source"] = "api"

                if now - self._last_orders_at >= 2.0:
                    self._cached_orders = client.get_open_orders() or []
                    self._last_orders_at = now
            except Exception as exc:
                logger.warning("Wallet Predict.fun poll failed: %s", exc)
                self._merge_health_wallet(ok=False, error=str(exc))
            else:
                self._merge_health_wallet(ok=True, error=None)
        else:
            self._merge_health_wallet(ok=False, error="Predict.fun not authenticated")

        # Always fetch on-chain USDT of funder_address (deposit address)
        if self.config.funder_address:
            try:
                usdt = fetch_onchain_usdt(self.config.bnb_rpc_url, self.config.funder_address)
                if usdt >= 0:
                    patch["balance_usdt"] = usdt
                    patch["onchain_usdt"] = usdt
                    patch["balance_source"] = "onchain_usdt"
            except Exception:
                pass

        # Map new field names to dashboard-compatible legacy names
        if "balance_usdt" in patch:
            patch["balance_usdc"] = patch["balance_usdt"]
        if "onchain_usdt" in patch:
            patch["onchain_usdc"] = patch["onchain_usdt"]
        if "gas_bnb" in patch:
            patch["gas_pol"] = patch["gas_bnb"]
        if "available_usdt" in patch:
            patch["available_usdc"] = patch["available_usdt"]

        patch["open_orders"] = self._cached_orders[:40]
        bal = patch.get("balance_usdt")
        if bal is not None:
            try:
                patch["available_usdt"] = max(0.0, float(bal))
            except (TypeError, ValueError):
                pass

        if self._signer and now - self._last_rpc_at >= 1.0:
            try:
                block, rtt = fetch_chain_head(self.config.bnb_rpc_url)
                patch["gas_bnb"] = fetch_bnb_balance(self.config.bnb_rpc_url, self._signer)
                if self.config.funder_address:
                    patch["onchain_usdt"] = fetch_onchain_usdt(
                        self.config.bnb_rpc_url, self.config.funder_address
                    )
                self._merge_health_rpc(
                    ok=True, error=None, block=block, rtt_ms=round(rtt * 1000.0, 1),
                )
                self._last_rpc_at = now
            except Exception as exc:
                logger.debug("Wallet RPC poll failed: %s", exc)
                self._merge_health_rpc(ok=False, error=str(exc))
                self._last_rpc_at = now

        patch["_stale_threshold_sec"] = self.config.wallet_balance_stale_sec
        self.state.merge_account(patch)
        if patch.get("open_orders") is not None:
            self.state.update(open_orders=patch["open_orders"][:20])

    async def run(self) -> None:
        while not self.state.should_shutdown():
            try:
                await asyncio.to_thread(self.poll_once)
            except Exception:
                logger.exception("Wallet monitor tick failed")
            await asyncio.sleep(self._interval)