import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BotConfig
from src.polymarket import PolymarketClient
from src.account import (
    fetch_onchain_usdc,
    fetch_onchain_native_usdc,
    fetch_onchain_pusd,
    fetch_pol_balance,
    fetch_pusd_exchange_allowance,
    USDC_E, USDC_NATIVE, PUSD
)
from py_clob_client_v2 import BalanceAllowanceParams, AssetType

def inspect_all():
    config = BotConfig.from_env()
    print("Funder:", config.funder_address)
    print("Signer:", config.private_key[:6] + "...")
    print("RPC URL:", config.polygon_rpc_url)

    pm = PolymarketClient(config, force_auth=True)
    print("\n--- CLOB Raw Responses ---")
    clob = pm._client
    try:
        res_col = clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        print("COLLATERAL balance_allowance:", json.dumps(res_col, indent=2))
    except Exception as e:
        print("COLLATERAL err:", e)

    try:
        res_cond = clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL))
        print("CONDITIONAL balance_allowance:", json.dumps(res_cond, indent=2))
    except Exception as e:
        print("CONDITIONAL err:", e)

    print("\n--- Polygon RPC Balances ---")
    funder = config.funder_address
    try:
        usdc_e = fetch_onchain_usdc(config.polygon_rpc_url, funder)
        print("On-chain USDC.e (0x2791...):", usdc_e)
    except Exception as e:
        print("USDC.e err:", e)

    try:
        usdc_n = fetch_onchain_native_usdc(config.polygon_rpc_url, funder)
        print("On-chain Native USDC (0x3c49...):", usdc_n)
    except Exception as e:
        print("USDC Native err:", e)

    try:
        pusd = fetch_onchain_pusd(config.polygon_rpc_url, funder)
        print("On-chain pUSD (0xC011...):", pusd)
    except Exception as e:
        print("pUSD err:", e)

    try:
        pol = fetch_pol_balance(config.polygon_rpc_url, funder)
        print("POL gas balance (Funder):", pol)
    except Exception as e:
        print("POL err:", e)

    try:
        pol_signer = fetch_pol_balance(config.polygon_rpc_url, pm.config.funder_address)
        print("POL gas balance (Signer):", pol_signer)
    except Exception as e:
        print("POL Signer err:", e)

if __name__ == "__main__":
    inspect_all()
