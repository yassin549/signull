import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BotConfig
from src.polymarket import PolymarketClient, _create_official_secure_client

config = BotConfig.from_env()
# A current active token ID on Polymarket (e.g. from current BTC candle)
from src.markets import get_current_candle
market = get_current_candle("btc")
if not market:
    print("No active market found")
    sys.exit(1)

print("Active market:", market.title, market.slug)
print("Up token:", market.up_token_id)

official = _create_official_secure_client(config.private_key, config.funder_address)
print("\n--- Testing Official Client Order Placement ---")
try:
    # Place a 1-share buy @ $0.01 (1 cent) so it sits far out of the money
    resp = official.place_limit_order(
        token_id=market.up_token_id,
        price=0.01,
        size=1.0,
        side="BUY",
    )
    print("Official order response:", resp)
    order_id = getattr(resp, "order_id", None) or getattr(resp, "id", None)
    if order_id:
        print("Canceling order:", order_id)
        print("Cancel result:", official.cancel_order(order_id=order_id))
except Exception as e:
    print("Official order placement ERROR:", e)

print("\n--- Testing ClobClient (py_clob_client_v2) Order Placement (SigType 3) ---")
config.signature_type = 3
pm = PolymarketClient(config, force_auth=True)
try:
    resp = pm._post_limit_buy(market.up_token_id, price=0.01, shares=1.0, tick_size=market.tick_size)
    print("ClobClient order response:", resp)
    order_id = resp.get("orderID") or resp.get("id")
    if order_id:
        print("Canceling ClobClient order:", order_id)
        print("Cancel result:", pm.cancel_order(order_id))
except Exception as e:
    print("ClobClient order placement ERROR:", e)
