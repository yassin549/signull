"""Predict.fun REST API + SDK client (replaces polymarket.py)."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_account.messages import encode_defunct

from predict_sdk import (
    ChainId,
    OrderBuilder,
    OrderBuilderOptions,
    Side,
    BuildOrderInput,
    LimitHelperInput,
    CancelOrdersOptions,
)

from .config import PREDICT_API_HOST, CHAIN_ID, BotConfig

_CHAIN_MAP = {
    "testnet": ChainId.BNB_TESTNET,
    "mainnet": ChainId.BNB_MAINNET,
}

def _resolve_chain_id() -> int:
    host = PREDICT_API_HOST.lower()
    if "testnet" in host:
        return ChainId.BNB_TESTNET
    if "mainnet" in host:
        return ChainId.BNB_MAINNET
    # Default: respect CHAIN_ID
    return ChainId.BNB_MAINNET if CHAIN_ID == 56 else ChainId.BNB_TESTNET

logger = logging.getLogger(__name__)

MIN_ORDER_USDT = 1.0


class PredictAuthError(Exception):
    pass


class PredictClient:
    def __init__(self, config: BotConfig, *, force_auth: bool = False):
        self.config = config
        self._api_key: str | None = None
        self._jwt: str | None = None
        self._jwt_expires_at: float = 0.0
        self._builder: OrderBuilder | None = None
        self._signer: LocalAccount | None = None
        self._authenticated = False
        self.auth_error: str | None = None

        self._http = httpx.Client(
            base_url=PREDICT_API_HOST,
            timeout=httpx.Timeout(15.0),
        )

        self._init_signer()
        self._set_api_key()
        if config.has_wallet and (config.is_live or force_auth):
            try:
                self._authenticate()
            except Exception as exc:
                if force_auth:
                    raise
                self.auth_error = str(exc)
                logger.warning("Predict.fun auth unavailable: %s", exc)

    def _init_signer(self) -> None:
        if not self.config.private_key:
            return
        pk = self.config.private_key
        if not pk.startswith("0x"):
            pk = f"0x{pk}"
        self._signer = Account.from_key(pk)

    def _set_api_key(self) -> None:
        is_testnet = "testnet" in PREDICT_API_HOST.lower()
        if not is_testnet:
            self._api_key = self.config.predict_api_key or ""

    def _authenticate(self) -> None:
        self._set_api_key()
        self._init_builder()
        if self._signer is None or not self.config.funder_address:
            return
        try:
            self._jwt = self._obtain_jwt()
            self._authenticated = True
        except Exception:
            self._jwt = None
            self._authenticated = False
            logger.warning("JWT obtain failed (public endpoints only)")

    def _obtain_jwt(self) -> str:
        assert self._signer is not None

        msg_resp = self._http.get("/v1/auth/message", headers=self._headers())
        msg_resp.raise_for_status()
        message = msg_resp.json()["data"]["message"]

        if self.config.is_predict_account and self._builder is not None:
            signature = self._builder.sign_predict_account_message(message)
            signer = self.config.funder_address
        else:
            signable = encode_defunct(text=message)
            signature = self._signer.sign_message(signable).signature.hex()
            signer = self._signer.address

        auth_resp = self._http.post(
            "/v1/auth",
            headers=self._headers(),
            json={"signer": signer, "message": message, "signature": signature},
        )
        auth_resp.raise_for_status()
        token = auth_resp.json()["data"]["token"]
        self._jwt_expires_at = time.time() + 3600
        return token

    def _init_builder(self) -> None:
        if self._signer is None:
            return
        opts = None
        if self.config.is_predict_account and self.config.funder_address:
            opts = OrderBuilderOptions(
                predict_account=self.config.funder_address,
            )
        self._builder = OrderBuilder.make(_resolve_chain_id(), self._signer, opts)

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["x-api-key"] = self._api_key
        if self._jwt:
            h["Authorization"] = f"Bearer {self._jwt}"
        return h

    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    def ensure_authenticated(self) -> bool:
        if self._authenticated:
            return True
        try:
            self._authenticate()
            return True
        except Exception as exc:
            self.auth_error = str(exc)
            logger.warning("Predict.fun auth unavailable: %s", exc)
            return False

    def verify_auth(self) -> bool:
        return self._authenticated

    def get_balance_usdt(self) -> float | None:
        if not self._authenticated or self._builder is None:
            return None
        try:
            bal_wei = self._builder.balance_of("USDT")
            return float(bal_wei) / 1e18
        except Exception as exc:
            logger.warning("Failed to fetch USDT balance: %s", exc)
            return None

    def get_open_orders(self) -> list[dict]:
        if not self._authenticated:
            return []
        try:
            resp = self._http.get("/v1/orders", headers=self._headers())
            resp.raise_for_status()
            data = resp.json().get("data", [])
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("Failed to fetch open orders: %s", exc)
            return []

    def get_positions(self) -> list[dict]:
        if not self._authenticated:
            return []
        try:
            resp = self._http.get("/v1/positions", headers=self._headers())
            resp.raise_for_status()
            data = resp.json().get("data", [])
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("Failed to fetch positions: %s", exc)
            return []

    def get_order(self, order_id: str) -> dict:
        if not order_id:
            return {}
        try:
            resp = self._http.get(f"/v1/orders/{order_id}", headers=self._headers())
            if resp.status_code == 200:
                return resp.json().get("data", {}) or resp.json()
            if resp.status_code == 404:
                # Individual order endpoint not supported — fallback to full list
                all_orders = self.get_open_orders()
                for order in all_orders:
                    oid = str(order.get("id") or order.get("orderId") or order.get("orderID") or "")
                    if oid == order_id:
                        return order
                    oid = str(order.get("orderHash") or "")
                    if oid and oid == order_id:
                        return order
                # Still 404 after fallback — return best-effort
                return {"status": "unknown", "id": order_id, "_fallback": True}
            return {}
        except Exception as exc:
            logger.warning("Failed to fetch order %s: %s", order_id, exc)
            return {}

    def cancel_order(self, order_id: str, order_data: dict | None = None) -> dict:
        if not self._authenticated:
            return {"error": "not authenticated"}
        try:
            resp = self._http.post(
                f"/v1/orders/{order_id}/cancel",
                headers=self._headers(),
            )
            if resp.status_code in (200, 201):
                return resp.json() if resp.content else {"success": True}
            if resp.status_code == 404:
                # Individual cancel endpoint not available — try batch cancel via SDK
                logger.info("Individual cancel 404 for %s — trying batch cancel", order_id)
                if self._builder is not None:
                    try:
                        orders_list = order_data or self.get_order(order_id)
                        if orders_list and orders_list.get("id"):
                            result = self._builder.cancel_orders(
                                [orders_list],
                                options=CancelOrdersOptions(is_neg_risk=False, is_yield_bearing=False),
                            )
                            if result and result.success:
                                return {"success": True}
                    except Exception:
                        pass
                # Batch cancel also unavailable — old order is NOT cancelled.
                # Return an error so the chase path does NOT place a new order on top.
                return {"error": "cancel unavailable via REST or SDK", "_note": "404"}
            return {"error": f"cancel returned {resp.status_code}: {resp.text}"}
        except Exception as exc:
            logger.warning("Failed to cancel order %s: %s", order_id, exc)
            return {"error": str(exc)}

    def cancel_orders_batch(self, orders, is_neg_risk=False, is_yield_bearing=False):
        if self._builder is None:
            return False
        try:
            result = self._builder.cancel_orders(
                orders,
                options=CancelOrdersOptions(is_neg_risk=is_neg_risk, is_yield_bearing=is_yield_bearing),
            )
            return result.success
        except Exception as exc:
            logger.warning("Batch cancel failed: %s", exc)
            return False

    def get_order_book(self, market_id: int) -> dict:
        try:
            resp = self._http.get(f"/v1/markets/{market_id}/orderbook", headers=self._headers())
            if resp.status_code == 200:
                return resp.json().get("data", {})
            logger.warning("Orderbook response %s for market %s", resp.status_code, market_id)
            return {"asks": [], "bids": []}
        except Exception as exc:
            logger.warning("Failed to fetch orderbook for market %s: %s", market_id, exc)
            return {"asks": [], "bids": []}

    def get_midpoint(self, market_id: int) -> float:
        book = self.get_order_book(market_id)
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = float(bids[0][0]) if bids else 0.0
        best_ask = float(asks[0][0]) if asks else 1.0
        if best_bid > 0 and best_ask < 1:
            return (best_bid + best_ask) / 2.0
        return 0.5

    def get_best_ask(self, market_id: int) -> float | None:
        book = self.get_order_book(market_id)
        asks = book.get("asks", [])
        if asks:
            return float(asks[0][0])
        return None

    def get_best_bid(self, market_id: int) -> float | None:
        book = self.get_order_book(market_id)
        bids = book.get("bids", [])
        if bids:
            return float(bids[0][0])
        return None

    def get_best_ask_for_side(self, market_id: int, side: str) -> float | None:
        """Marketable ask for the outcome being bought.

        The market orderbook is the UP token's book: UP asks are the ask levels,
        and the DOWN token is the complement, so the DOWN ask is 1 - up_best_bid.
        """
        book = self.get_order_book(market_id)
        asks = book.get("asks", [])
        bids = book.get("bids", [])
        if str(side).lower() == "down":
            if bids:
                return round(1.0 - float(bids[0][0]), 2)
            return None
        if asks:
            return float(asks[0][0])
        return None

    def place_limit_buy(
        self,
        market_id: int,
        token_id: str,
        price: float,
        size_usdt: float,
        fee_rate_bps: int = 200,
        is_neg_risk: bool = False,
        is_yield_bearing: bool = False,
        decimal_precision: int = 2,
    ) -> dict:
        if self._builder is None:
            return {"error": "OrderBuilder not initialized"}
        if not self._authenticated:
            return {"error": "not authenticated"}

        price = round(max(0.01, min(0.99, float(price))), decimal_precision)
        if size_usdt < MIN_ORDER_USDT:
            size_usdt = MIN_ORDER_USDT
            logger.info("Bumped order to minimum $%.2f USDT", size_usdt)

        shares = size_usdt / price
        shares_wei = int(shares * 1e18)
        price_wei = int(price * 1e18)

        amounts = self._builder.get_limit_order_amounts(
            LimitHelperInput(side=Side.BUY, price_per_share_wei=price_wei, quantity_wei=shares_wei),
        )

        maker = self._signer.address if self._signer else ""
        signer_addr = maker

        order = self._builder.build_order(
            "LIMIT",
            BuildOrderInput(
                side=Side.BUY,
                token_id=token_id,
                maker=maker,
                signer=signer_addr,
                maker_amount=str(amounts.maker_amount),
                taker_amount=str(amounts.taker_amount),
                fee_rate_bps=fee_rate_bps,
            ),
        )

        typed_data = self._builder.build_typed_data(
            order, is_neg_risk=is_neg_risk, is_yield_bearing=is_yield_bearing,
        )

        signed_order = self._builder.sign_typed_data_order(typed_data)
        order_hash = self._builder.build_typed_data_hash(typed_data)

        d = {k: v for k, v in vars(signed_order).items() if v is not None}
        d["side"] = int(d["side"])
        d["signature_type"] = int(d["signature_type"])
        d["hash"] = order_hash

        km = {
            "token_id": "tokenId",
            "maker_amount": "makerAmount",
            "taker_amount": "takerAmount",
            "fee_rate_bps": "feeRateBps",
            "signature_type": "signatureType",
        }
        order_body = {km.get(k, k): v for k, v in d.items()}

        body = {
            "data": {
                "order": order_body,
                "pricePerShare": str(price_wei),
                "strategy": "LIMIT",
            }
        }

        logger.info("Placing BUY %s shares @ $%.2f (≈$%.2f USDT)", round(shares, decimal_precision), price, size_usdt)

        try:
            resp = self._http.post("/v1/orders", headers=self._headers(), json=body)
            if resp.status_code in (200, 201):
                result = resp.json()
                data = result.get("data", {}) or result
                order_id = (
                    data.get("orderHash", "")
                    or data.get("orderId", "")
                    or data.get("id", "")
                    or result.get("orderId", "")
                    or result.get("orderID", "")
                    or result.get("id", "")
                )
                if not order_id and result.get("data", {}).get("order"):
                    order_data = result["data"]["order"]
                    order_id = (
                        order_data.get("orderHash", "")
                        or order_data.get("id", "")
                        or order_data.get("orderId", "")
                    )
                if order_id and not order_id.startswith("0x"):
                    numeric_id = order_id
                    order_hash = data.get("orderHash", "")
                    if order_hash:
                        order_id = order_hash
                return {"orderID": order_id, "success": True, "raw": result}
            detail = resp.text
            logger.warning("Order placement failed: %s", detail)
            return {"error": detail}
        except Exception as exc:
            logger.warning("Order placement exception: %s", exc)
            return {"error": str(exc)}

    def get_market(self, market_id: int) -> dict:
        try:
            resp = self._http.get(f"/v1/markets/{market_id}", headers=self._headers())
            resp.raise_for_status()
            return resp.json().get("data", {})
        except Exception:
            return {}

    def get_category(self, slug: str) -> dict:
        try:
            resp = self._http.get(f"/v1/categories/{slug}", headers=self._headers())
            if resp.status_code == 200:
                return resp.json().get("data", {})
            return {}
        except Exception:
            return {}

    def get_categories(self, params: dict | None = None) -> list[dict]:
        try:
            resp = self._http.get("/v1/categories", params=params, headers=self._headers())
            resp.raise_for_status()
            data = resp.json().get("data", [])
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("Failed to fetch categories: %s", exc)
            return []

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PredictClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()