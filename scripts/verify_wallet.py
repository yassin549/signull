"""Verify your Polymarket wallet connection before going live."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.account import verify_wallet
from src.config import BotConfig


def main() -> None:
    print("=" * 60)
    print("  Signull — Polymarket Wallet Verification")
    print("=" * 60)
    print()

    try:
        config = BotConfig.from_env()
    except ValueError as exc:
        print(f"Config error: {exc}")
        sys.exit(1)

    result = verify_wallet(config)

    print(f"  Signature type : {result.signature_type} ({result.signature_label})")
    print(f"  Signer address : {result.signer_address or '—'}")
    print(f"  Funder address : {result.funder_address or '—'}")
    print(f"  API connected  : {'YES' if result.api_connected else 'NO'}")
    if result.balance_usdc is not None:
        print(f"  USDC balance   : ${result.balance_usdc:.2f}")
    print()

    if result.issues:
        print("  ISSUES:")
        for issue in result.issues:
            print(f"    ✗ {issue}")
        print()

    if result.tips:
        print("  NEXT STEPS:")
        for tip in result.tips:
            print(f"    → {tip}")
        print()

    if result.ok:
        print("  ✓ Wallet is connected and ready for live trading!")
        print("    Set TRADING_MODE=live in .env when you're ready.")
    else:
        print("  ✗ Wallet not ready yet. Fix the issues above.")
        sys.exit(1)


if __name__ == "__main__":
    main()