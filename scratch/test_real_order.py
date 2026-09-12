import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BotConfig
from polymarket import SecureClient
from src.markets import get_current_candle

config = BotConfig.from_env()
market = get_current_candle("btc")
if not market:
    print("No active market found")
    sys.exit(1)

deposit_wallet = "0x5d7Df1D4c1D35d2695C7fA0EB13aE680f4095953"
print("Market:", market.title, market.slug)
print("Token:", market.up_token_id)

client = SecureClient.create(
    private_key=config.private_key,
    wallet=deposit_wallet,
)
print("Connected wallet type:", client.wallet_type)
print("Connected wallet:", client.wallet)

print("\n--- Testing Order Placement (1 share @ $0.01) ---")
try:
    resp = client.place_limit_order(
        token_id=market.up_token_id,
        price=0.01,
        size=1.0,
        side="BUY",
    )
    print("Order response:", resp)
    order_id = str(getattr(resp, "order_id", "") or getattr(resp, "id", "") or "")
    print("Order ID:", order_id)
    if order_id:
        print("Canceling order...")
        canc = client.cancel_order(order_id=order_id)
        print("Cancel response:", canc)
        print("SUCCESS! Order placed and canceled clean!")
except Exception as e:
    print("Order placement ERROR:", e)
