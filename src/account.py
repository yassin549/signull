"""Wallet verification and account data helpers — BNB Chain (Predict.fun)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests
from eth_account import Account

from .config import BotConfig, PREDICT_API_HOST

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "signull-bot/0.1"}

USDT = "0x55d398326f99059fF775485246999027B3197955"  # BNB Chain USDT (BSC)
BNB_RPC_DEFAULT = "https://bsc-dataseed.binance.org"

_BALANCE_OF_SELECTOR = "70a08231"


@dataclass
class WalletCheck:
    ok: bool
    signer_address: str | None
    funder_address: str | None
    signature_type: int
    signature_label: str
    api_connected: bool
    balance_usdt: float | None
    issues: list[str]
    tips: list[str]
    balance_source: str = "api"
    gas_bnb: float | None = None
    rpc_ok: bool = False
    rpc_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "signer_address": self.signer_address,
            "funder_address": self.funder_address,
            "signature_type": self.signature_type,
            "signature_label": self.signature_label,
            "api_connected": self.api_connected,
            "balance_usdt": self.balance_usdt,
            "balance_usdc": self.balance_usdt,
            "balance_source": self.balance_source,
            "gas_bnb": self.gas_bnb,
            "gas_pol": self.gas_bnb,
            "rpc_ok": self.rpc_ok,
            "rpc_error": self.rpc_error,
            "issues": self.issues,
            "tips": self.tips,
        }


def _derive_signer_address(private_key: str) -> str:
    key = private_key if private_key.startswith("0x") else f"0x{private_key}"
    return Account.from_key(key).address


def bnb_rpc_call(
    rpc_url: str,
    method: str,
    params: list,
    *,
    timeout: float = 5.0,
) -> object:
    resp = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers=HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return payload.get("result")


def fetch_bnb_balance(rpc_url: str, address: str) -> float:
    raw = bnb_rpc_call(rpc_url, "eth_getBalance", [address, "latest"])
    return int(str(raw), 16) / 1e18


def fetch_erc20_balance(rpc_url: str, token: str, address: str) -> float:
    addr = address.lower().replace("0x", "").zfill(64)
    data = "0x" + _BALANCE_OF_SELECTOR + addr
    raw = bnb_rpc_call(
        rpc_url,
        "eth_call",
        [{"to": token, "data": data}, "latest"],
    )
    return int(str(raw), 16) / 1e18


def fetch_onchain_usdt(rpc_url: str, address: str) -> float:
    return fetch_erc20_balance(rpc_url, USDT, address)


def fetch_chain_head(rpc_url: str) -> tuple[int, float]:
    t0 = time.time()
    raw = bnb_rpc_call(rpc_url, "eth_blockNumber", [])
    rtt = time.time() - t0
    return int(str(raw), 16), rtt


def verify_wallet(config: BotConfig) -> WalletCheck:
    issues: list[str] = []
    tips: list[str] = []
    signer: str | None = None
    api_connected = False
    balance: float | None = None
    gas_bnb: float | None = None
    rpc_ok = False
    rpc_error: str | None = None

    if not config.private_key:
        issues.append("PRIVATE_KEY is missing from .env")
        tips.append("Export your wallet private key and add PRIVATE_KEY=0x... to .env")
    else:
        try:
            signer = _derive_signer_address(config.private_key)
        except Exception:
            issues.append("PRIVATE_KEY is invalid")
            tips.append("Private key must be a 64-char hex string, optionally prefixed with 0x")

    if not config.funder_address:
        issues.append("FUNDER_ADDRESS is missing from .env")
        tips.append("Copy your deposit address from Predict.fun into FUNDER_ADDRESS=0x...")

    if config.private_key and config.funder_address and signer is not None:
        try:
            from .predict_client import PredictClient
            client = PredictClient(config, force_auth=True)
            api_connected = client.verify_auth()
            balance = client.get_balance_usdt()
            if balance is None:
                balance = 0.0
        except Exception as exc:
            issues.append(f"Predict.fun API auth failed: {exc}")
            tips.append("Double-check PREDICT_API_KEY and FUNDER_ADDRESS")

    rpc_url = config.bnb_rpc_url
    if rpc_url:
        try:
            gas_bnb = fetch_bnb_balance(rpc_url, signer)
            rpc_ok = True
        except Exception as exc:
            rpc_error = str(exc)
            logger.debug("BNB RPC probe failed: %s", exc)
            tips.append("On-chain BNB/USDT unavailable — set BNB_RPC_URL if the default is rate-limited")

    if balance is not None and balance <= 0:
        if hasattr(config, 'funder_address') and config.funder_address:
            try:
                onchain = fetch_onchain_usdt(rpc_url, config.funder_address)
                if onchain > 0:
                    balance = onchain
                    tips.append("Using on-chain USDT balance (API returned $0)")
            except Exception:
                pass
        if balance <= 0:
            issues.append("Wallet has $0 USDT trading balance")
            tips.append("Deposit USDT to your Predict.fun wallet before live trading")

    blocking = [i for i in issues if not i.startswith("Low BNB")]
    ok = len(blocking) == 0 and api_connected
    return WalletCheck(
        ok=ok,
        signer_address=signer,
        funder_address=config.funder_address,
        signature_type=0,
        signature_label="EOA",
        api_connected=api_connected,
        balance_usdt=balance,
        gas_bnb=gas_bnb,
        rpc_ok=rpc_ok,
        rpc_error=rpc_error,
        issues=issues,
        tips=tips,
    )


def fetch_positions(funder_address: str) -> list[dict]:
    try:
        resp = requests.get(
            f"{PREDICT_API_HOST}/v1/positions",
            params={"user": funder_address},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception:
        return []