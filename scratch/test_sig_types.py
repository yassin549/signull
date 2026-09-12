import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import BotConfig
from py_clob_client_v2 import ClobClient, SignatureTypeV2, BalanceAllowanceParams, AssetType

config = BotConfig.from_env()
creds = ClobClient(
    "https://clob.polymarket.com",
    key=config.private_key,
    chain_id=137
).create_or_derive_api_key()

for sig_type_num, sig_enum in [
    (0, SignatureTypeV2.EOA),
    (1, SignatureTypeV2.POLY_PROXY),
    (2, SignatureTypeV2.POLY_GNOSIS_SAFE),
    (3, SignatureTypeV2.POLY_1271),
]:
    clob = ClobClient(
        "https://clob.polymarket.com",
        key=config.private_key,
        chain_id=137,
        creds=creds,
        signature_type=sig_enum,
        funder=config.funder_address,
    )
    try:
        res = clob.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        print(f"SigType {sig_type_num} ({sig_enum}):", res)
    except Exception as e:
        print(f"SigType {sig_type_num} ({sig_enum}) ERROR:", e)
