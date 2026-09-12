import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BotConfig
from src.polymarket import PolymarketClient
from src.account import verify_wallet

# Force override funder address to the derived deposit wallet
config = BotConfig.from_env()
config.funder_address = "0x5d7Df1D4c1D35d2695C7fA0EB13aE680f4095953"
config.signature_type = 3

print("--- Testing verify_wallet ---")
check = verify_wallet(config)
print("Verified OK:", check.ok)
print("Signer:", check.signer_address)
print("Funder:", check.funder_address)
print("Signature type:", check.signature_type, check.signature_label)
print("Balance USDC:", check.balance_usdc)
print("Allowance USDC:", check.allowance_usdc)
print("Issues:", check.issues)

print("\n--- Testing PolymarketClient ---")
pm = PolymarketClient(config, force_auth=True)
print("PM authenticated:", pm.is_authenticated)
print("PM config sig_type:", pm.config.signature_type)
print("PM snapshot:", pm.get_collateral_snapshot())
