import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BotConfig
from src.polymarket import PolymarketClient, _create_official_secure_client
from src.account import verify_wallet, fetch_onchain_pusd, fetch_onchain_usdc

def test_wallet():
    config = BotConfig.from_env()
    print("Config from env:")
    print("  TRADING_MODE:", config.trading_mode)
    print("  SIGNATURE_TYPE:", config.signature_type)
    print("  FUNDER_ADDRESS:", config.funder_address)
    print("  PRIVATE_KEY set:", bool(config.private_key))

    print("\n--- Verifying Wallet ---")
    check = verify_wallet(config)
    print("Check OK:", check.ok)
    print("Signer:", check.signer_address)
    print("Funder:", check.funder_address)
    print("Signature type after check:", check.signature_type, check.signature_label)
    print("Balance USDC:", check.balance_usdc)
    print("Allowance USDC:", check.allowance_usdc)
    print("Issues:", check.issues)
    print("Tips:", check.tips)

    print("\n--- Testing Official Client ---")
    try:
        official = _create_official_secure_client(config.private_key, config.funder_address)
        print("Official client created successfully!")
        print("Official wallet_type:", getattr(official, "wallet_type", None))
        print("Official wallet:", getattr(official, "wallet", None))
    except Exception as e:
        print("Official client creation error:", e)

    print("\n--- Testing PolymarketClient ---")
    pm = PolymarketClient(config, force_auth=True)
    print("PM authenticated:", pm.is_authenticated)
    print("PM signature_type:", pm.config.signature_type)
    print("PM collateral snapshot:", pm.get_collateral_snapshot())

if __name__ == "__main__":
    test_wallet()
