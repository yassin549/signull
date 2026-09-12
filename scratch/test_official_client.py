import sys
import inspect
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BotConfig
from src.polymarket import PolymarketClient, _create_official_secure_client

config = BotConfig.from_env()
official = _create_official_secure_client(config.private_key, config.funder_address)
print("Official client type:", type(official))
print("Official attributes/methods:")
for name in dir(official):
    if not name.startswith("_"):
        print(" -", name)

print("\nOfficial wallet_type:", getattr(official, "wallet_type", None))
print("Official wallet:", getattr(official, "wallet", None))

sig = inspect.signature(official.place_limit_order)
print("place_limit_order signature:", sig)
