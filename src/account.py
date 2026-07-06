"""Wallet verification and account data helpers."""

from __future__ import annotations

from dataclasses import dataclass

import requests
from eth_account import Account

from .config import DATA_HOST, BotConfig
from .polymarket import PolymarketClient

HEADERS = {"User-Agent": "signull-bot/0.1"}


@dataclass
class WalletCheck:
    ok: bool
    signer_address: str | None
    funder_address: str | None
    signature_type: int
    signature_label: str
    api_connected: bool
    balance_usdc: float | None
    issues: list[str]
    tips: list[str]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "signer_address": self.signer_address,
            "funder_address": self.funder_address,
            "signature_type": self.signature_type,
            "signature_label": self.signature_label,
            "api_connected": self.api_connected,
            "balance_usdc": self.balance_usdc,
            "issues": self.issues,
            "tips": self.tips,
        }


def _derive_signer_address(private_key: str) -> str:
    key = private_key if private_key.startswith("0x") else f"0x{private_key}"
    return Account.from_key(key).address


def verify_wallet(config: BotConfig) -> WalletCheck:
    issues: list[str] = []
    tips: list[str] = []
    signer: str | None = None
    api_connected = False
    balance: float | None = None

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
        tips.append(
            "Go to https://polymarket.com/settings and copy your Profile Address "
            "into FUNDER_ADDRESS=0x..."
        )

    if config.signature_type == 0:
        tips.append(
            "EOA users must set token allowances on Polygon before live trading. "
            "Email/Google login users (type 1) get allowances automatically."
        )
    elif config.signature_type == 1:
        tips.append(
            "For email/Google login: export your Magic wallet private key and set "
            "FUNDER_ADDRESS to your Polymarket profile address (not your signer EOA)."
        )
    elif config.signature_type == 3:
        tips.append(
            "Deposit wallets require extra setup: deploy wallet, fund with pUSD, "
            "and approve contracts. See docs.polymarket.com/trading/deposit-wallets"
        )

    if config.private_key and config.funder_address and not issues:
        try:
            client = PolymarketClient(config, force_auth=True)
            api_connected = client.verify_auth()
            balance = client.get_collateral_balance()
            if balance is not None and balance <= 0:
                issues.append("Funder wallet has $0 USDC balance")
                tips.append("Deposit USDC to your Polymarket wallet before live trading")
        except Exception as exc:
            issues.append(f"CLOB auth failed: {exc}")
            tips.append(
                "Double-check SIGNATURE_TYPE matches your account type and "
                "FUNDER_ADDRESS matches polymarket.com/settings"
            )

    ok = len(issues) == 0 and api_connected
    return WalletCheck(
        ok=ok,
        signer_address=signer,
        funder_address=config.funder_address,
        signature_type=config.signature_type,
        signature_label=config.signature_label,
        api_connected=api_connected,
        balance_usdc=balance,
        issues=issues,
        tips=tips,
    )


def fetch_positions(funder_address: str) -> list[dict]:
    resp = requests.get(
        f"{DATA_HOST}/positions",
        params={"user": funder_address, "sizeThreshold": 0},
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()