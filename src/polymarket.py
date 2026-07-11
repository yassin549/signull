"""Thin wrapper around the Polymarket CLOB client."""

from __future__ import annotations

import logging

from py_clob_client_v2 import (
    AssetType,
    BalanceAllowanceParams,
    ClobClient,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    SignatureTypeV2,
)
from py_clob_client_v2.order_builder.constants import BUY

from .config import BotConfig, CHAIN_ID, CLOB_HOST

logger = logging.getLogger(__name__)


class PolymarketClient:
    def __init__(self, config: BotConfig, *, force_auth: bool = False):
        self.config = config
        self._client: ClobClient | None = None
        self._authenticated = False
        self.auth_error: str | None = None

        # Paper trading only needs the public market-data client.  In
        # particular, do not derive CLOB credentials at server startup just
        # because a developer happens to have wallet variables in ``.env``.
        # Credential derivation is a network call and a transient failure used
        # to prevent even the paper dashboard from starting.
        if config.has_wallet and (config.is_live or force_auth):
            try:
                self._client = self._build_trading_client()
                self._authenticated = True
            except Exception as exc:
                # Keep the market-data server and dashboard available when the
                # CLOB is slow or wallet settings are invalid.  The bot will
                # refuse live orders while unauthenticated.  Explicit wallet
                # verification still raises so it can report the real cause.
                if force_auth:
                    raise
                self.auth_error = str(exc)
                logger.warning("CLOB authentication unavailable: %s", exc)
                self._client = ClobClient(CLOB_HOST, chain_id=CHAIN_ID)
        else:
            self._client = ClobClient(CLOB_HOST, chain_id=CHAIN_ID)

    def _build_trading_client(self) -> ClobClient:
        assert self.config.private_key and self.config.funder_address

        temp = ClobClient(
            CLOB_HOST,
            key=self.config.private_key,
            chain_id=CHAIN_ID,
        )
        creds = temp.create_or_derive_api_key()

        sig_map = {
            0: SignatureTypeV2.EOA,
            1: SignatureTypeV2.POLY_PROXY,
            2: SignatureTypeV2.POLY_GNOSIS_SAFE,
            3: SignatureTypeV2.POLY_1271,
        }
        sig_type = sig_map.get(self.config.signature_type, SignatureTypeV2.POLY_PROXY)

        return ClobClient(
            CLOB_HOST,
            key=self.config.private_key,
            chain_id=CHAIN_ID,
            creds=creds,
            signature_type=sig_type,
            funder=self.config.funder_address,
        )

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    def verify_auth(self) -> bool:
        assert self._client is not None
        self._client.get_ok()
        self._client.get_server_time()
        return True

    def get_collateral_balance(self) -> float | None:
        if not self._authenticated:
            return None
        assert self._client is not None
        try:
            result = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            # Balance is returned in micro-USDC (6 decimals)
            raw = float(result.get("balance", 0))
            return raw / 1_000_000
        except Exception:
            logger.exception("Failed to fetch balance")
            return None

    def get_open_orders(self) -> list[dict]:
        if not self._authenticated:
            return []
        assert self._client is not None
        try:
            return self._client.get_open_orders() or []
        except Exception:
            logger.exception("Failed to fetch open orders")
            return []

    def get_order(self, order_id: str) -> dict:
        if not self._authenticated or not order_id:
            return {}
        assert self._client is not None
        return self._client.get_order(order_id) or {}

    def cancel_order(self, order_id: str) -> dict:
        if not self._authenticated or not order_id:
            return {}
        assert self._client is not None
        return self._client.cancel_order(order_id) or {}

    def get_order_book(self, token_id: str):
        assert self._client is not None
        return self._client.get_order_book(token_id)

    def get_midpoint(self, token_id: str) -> float:
        assert self._client is not None
        result = self._client.get_midpoint(token_id)
        return float(result.get("mid", result) if isinstance(result, dict) else result)

    def get_best_ask(self, token_id: str) -> float | None:
        assert self._client is not None
        result = self._client.get_price(token_id, side="BUY")
        if result is None:
            return None
        return float(result.get("price", result) if isinstance(result, dict) else result)

    def place_limit_buy(
        self,
        token_id: str,
        price: float,
        size_usdc: float,
        tick_size: str,
    ) -> dict:
        """Place a GTC limit buy. size_usdc is the dollar amount to spend."""
        assert self._client is not None
        shares = round(size_usdc / price, 2)

        logger.info(
            "Placing BUY %s shares @ $%.2f (≈$%.2f USDC)",
            shares,
            price,
            size_usdc,
        )

        return self._client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=price,
                size=shares,
                side=BUY,
            ),
            options=PartialCreateOrderOptions(
                tick_size=tick_size,
                neg_risk=False,
            ),
            order_type=OrderType.GTC,
        )

    def send_heartbeat(self, heartbeat_id: str = "") -> str:
        """Keep session alive; orders auto-cancel if heartbeats stop."""
        assert self._client is not None
        result = self._client.post_heartbeat(heartbeat_id)
        return result.get("heartbeat_id", heartbeat_id)
