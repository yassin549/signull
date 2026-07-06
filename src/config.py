import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
CHAIN_ID = 137

SERIES_SLUGS = {
    "btc": "btc-up-or-down-5m",
    "eth": "eth-up-or-down-5m",
    "sol": "sol-up-or-down-5m",
    "xrp": "xrp-up-or-down-5m",
}

# Prefix used in per-candle event slugs (e.g. btc-updown-5m-1783179600)
EVENT_SLUG_PREFIX = {
    "btc": "btc",
    "eth": "eth",
    "sol": "sol",
    "xrp": "xrp",
}


DATA_HOST = "https://data-api.polymarket.com"

SIGNATURE_TYPE_LABELS = {
    0: "EOA (MetaMask)",
    1: "Proxy (Email/Google)",
    2: "Gnosis Safe",
    3: "Deposit Wallet (API)",
}


@dataclass
class BotConfig:
    trading_mode: str
    asset: str
    order_size_usdc: float
    max_entry_price: float
    poll_interval_sec: float
    private_key: str | None
    funder_address: str | None
    signature_type: int
    server_host: str
    server_port: int
    dashboard_push_ms: int
    bot_poll_interval_sec: float

    @classmethod
    def from_env(cls) -> "BotConfig":
        asset = os.getenv("ASSET", "btc").lower()
        if asset not in SERIES_SLUGS:
            raise ValueError(f"ASSET must be one of {list(SERIES_SLUGS)}")

        mode = os.getenv("TRADING_MODE", "paper").lower()
        if mode not in ("paper", "live"):
            raise ValueError("TRADING_MODE must be 'paper' or 'live'")

        funder = os.getenv("FUNDER_ADDRESS") or os.getenv("DEPOSIT_WALLET_ADDRESS")

        return cls(
            trading_mode=mode,
            asset=asset,
            order_size_usdc=float(os.getenv("ORDER_SIZE_USDC", "5.0")),
            max_entry_price=float(os.getenv("MAX_ENTRY_PRICE", "0.55")),
            poll_interval_sec=float(os.getenv("POLL_INTERVAL_SEC", "2")),
            private_key=os.getenv("PRIVATE_KEY"),
            funder_address=funder,
            signature_type=int(os.getenv("SIGNATURE_TYPE", "1")),
            server_host=os.getenv("SERVER_HOST", "127.0.0.1"),
            server_port=int(os.getenv("SERVER_PORT", "8080")),
            dashboard_push_ms=int(os.getenv("DASHBOARD_PUSH_MS", "50")),
            bot_poll_interval_sec=float(os.getenv("BOT_POLL_INTERVAL_SEC", "2")),
        )

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def has_wallet(self) -> bool:
        return bool(self.private_key and self.funder_address)

    @property
    def signature_label(self) -> str:
        return SIGNATURE_TYPE_LABELS.get(self.signature_type, "Unknown")