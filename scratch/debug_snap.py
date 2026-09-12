import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BotConfig
from src.polymarket import PolymarketClient

config = BotConfig.from_env()
pm = PolymarketClient(config, force_auth=True)

print("Client signature_type:", pm.config.signature_type)
print("Client auth error:", pm.auth_error)
print("Client _client sig_type:", pm._client.builder.signature_type if pm._client else None)
print("Client _client funder:", pm._client.builder.funder if pm._client else None)

snap = pm.get_collateral_snapshot()
print("Snapshot:", snap)

res = pm._client.get_balance_allowance(asset_type="COLLATERAL")
print("Direct call result:", res)
