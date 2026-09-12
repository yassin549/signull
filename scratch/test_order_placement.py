import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BotConfig
from src.polymarket import PolymarketClient, _create_official_secure_client

config = BotConfig.from_env()
print("Config SIGNATURE_TYPE:", config.signature_type)

official = _create_official_secure_client(config.private_key, config.funder_address)
print("Official wallet_type:", getattr(official, "wallet_type", None))
print("Official wallet:", getattr(official, "wallet", None))

pm = PolymarketClient(config, force_auth=True)
print("PM config sig_type:", pm.config.signature_type)
print("PM authenticated:", pm.is_authenticated)

# Let's check open orders or test order placement logic
print("Open orders via official:", getattr(official, "list_open_orders", lambda: [])())
print("Open orders via PM:", pm.get_open_orders())
