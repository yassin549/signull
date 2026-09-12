import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BotConfig
from polymarket import SecureClient

config = BotConfig.from_env()

print("--- Testing SecureClient with wallet=FUNDER_ADDRESS ---")
try:
    client_funder = SecureClient.create(
        private_key=config.private_key,
        wallet=config.funder_address,
    )
    print("wallet_type:", client_funder.wallet_type)
    print("wallet:", client_funder.wallet)
except Exception as e:
    print("Error:", e)

print("\n--- Testing SecureClient with wallet=None ---")
try:
    client_default = SecureClient.create(
        private_key=config.private_key,
        wallet=None,
    )
    print("wallet_type:", client_default.wallet_type)
    print("wallet:", client_default.wallet)
except Exception as e:
    print("Error:", e)
