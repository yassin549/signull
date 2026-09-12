import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BotConfig
from polymarket import SecureClient
from py_clob_client_v2 import ClobClient, SignatureTypeV2, BalanceAllowanceParams, AssetType

config = BotConfig.from_env()

# Test SecureClient with the real Deposit Wallet address
deposit_wallet = "0x5d7Df1D4c1D35d2695C7fA0EB13aE680f4095953"

print("--- Testing SecureClient with Deposit Wallet ---")
client = SecureClient.create(
    private_key=config.private_key,
    wallet=deposit_wallet,
)
print("Wallet type:", client.wallet_type)
print("Wallet:", client.wallet)

# Let's check balance via SecureClient / CLOB
try:
    snap = client.get_balance_allowance(asset_type="COLLATERAL")
    print("SecureClient get_balance_allowance:", snap)
except Exception as e:
    print("SecureClient snap error:", e)

print("\n--- Testing ClobClient (py_clob_client_v2) with Deposit Wallet (SigType 3) ---")
creds = ClobClient(
    "https://clob.polymarket.com",
    key=config.private_key,
    chain_id=137
).create_or_derive_api_key()

clob3 = ClobClient(
    "https://clob.polymarket.com",
    key=config.private_key,
    chain_id=137,
    creds=creds,
    signature_type=SignatureTypeV2.POLY_1271,
    funder=deposit_wallet,
)
try:
    res = clob3.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print("SigType 3 (POLY_1271) balance_allowance:", res)
except Exception as e:
    print("SigType 3 error:", e)
