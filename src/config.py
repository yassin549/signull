import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

PREDICT_API_HOST = os.getenv(
    "PREDICT_API_HOST", "https://api-testnet.predict.fun"
)
PREDICT_WS_URL = ""
CHAIN_ID = 56  # BNB Mainnet

# Polymarket public APIs — used by the backtest data loader for historical
# 5-minute outcome-token price history (Predict.fun has no public history API).
GAMMA_HOST = os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com")
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
DATA_HOST = os.getenv("DATA_HOST", "https://data-api.polymarket.com")

SERIES_SLUGS = {
    "btc": "btc-updown-5m",
    "eth": "eth-updown-5m",
    "sol": "sol-updown-5m",
    "xrp": "xrp-updown-5m",
}

EVENT_SLUG_PREFIX = {
    "btc": "btc",
    "eth": "eth",
    "sol": "sol",
    "xrp": "xrp",
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
    predict_api_key: str | None
    is_predict_account: bool
    server_host: str
    server_port: int
    dashboard_push_ms: int
    bot_poll_interval_sec: float
    paper_initial_capital: float
    strategy_threshold: float
    strategy_min_risk_pct: float
    strategy_max_risk_pct: float
    strategy_trust_lookback: int
    strategy_btc_align_min: float
    strategy_big_equity_buffer: float
    strategy_risk_pct: float
    strategy_id: str = "signull_1_5"
    custom_strategy_params: dict | None = None
    bnb_rpc_url: str = "https://bsc-dataseed.binance.org"
    wallet_poll_ms: int = 1000
    gas_warn_bnb: float = 0.01
    fixed_stake_usdc: float = 1.0
    use_fixed_stake: bool = True
    max_chase_attempts: int = 5
    chase_interval_sec: float = 5.0
    max_heartbeat_failures: int = 3
    maker_fee_rate: float = 0.0
    taker_fee_rate: float = 0.02
    wallet_balance_stale_sec: float = 60.0

    @classmethod
    def from_env(cls) -> "BotConfig":
        asset = os.getenv("ASSET", "btc").lower()
        if asset not in SERIES_SLUGS:
            raise ValueError(f"ASSET must be one of {list(SERIES_SLUGS)}")

        mode = os.getenv("TRADING_MODE", "paper").lower()
        if mode not in ("paper", "live"):
            raise ValueError("TRADING_MODE must be 'paper' or 'live'")
        strategy_id = os.getenv("BOT_STRATEGY", "signull_1_5").lower()

        funder = os.getenv("FUNDER_ADDRESS") or os.getenv("PREDICT_ACCOUNT_ADDRESS")
        fixed_stake_env = os.getenv("FIXED_STAKE_USDC")
        order_size_env = os.getenv("ORDER_SIZE_USDC")
        fixed_stake = float(fixed_stake_env or order_size_env or "1.0")
        use_fixed_env = os.getenv("USE_FIXED_STAKE", "true").lower()
        use_fixed = use_fixed_env in ("true", "1", "yes")

        api_key = os.getenv("PREDICT_API_KEY")
        is_predict_acct = os.getenv("IS_PREDICT_ACCOUNT", "false").lower() in ("true", "1", "yes")

        return cls(
            trading_mode=mode,
            asset=asset,
            order_size_usdc=fixed_stake,
            max_entry_price=float(os.getenv("MAX_ENTRY_PRICE", "0.55")),
            poll_interval_sec=float(os.getenv("POLL_INTERVAL_SEC", "2")),
            private_key=os.getenv("PRIVATE_KEY"),
            funder_address=funder,
            predict_api_key=api_key,
            is_predict_account=is_predict_acct,
            server_host=os.getenv("SERVER_HOST", "127.0.0.1"),
            server_port=int(os.getenv("SERVER_PORT", "8080")),
            dashboard_push_ms=int(os.getenv("DASHBOARD_PUSH_MS", "250")),
            bot_poll_interval_sec=float(os.getenv("BOT_POLL_INTERVAL_SEC", "2")),
            paper_initial_capital=float(os.getenv("PAPER_INITIAL_CAPITAL", "100")),
            strategy_threshold=float(os.getenv("SIGNULL_THRESHOLD", "0.75")),
            strategy_min_risk_pct=float(os.getenv("SIGNULL_MIN_RISK_PCT", "0.05")),
            strategy_max_risk_pct=float(os.getenv("SIGNULL_MAX_RISK_PCT", "0.50")),
            strategy_trust_lookback=int(os.getenv("SIGNULL_TRUST_LOOKBACK", "3")),
            strategy_btc_align_min=float(os.getenv("SIGNULL_BTC_ALIGN_MIN", "0.55")),
            strategy_big_equity_buffer=float(os.getenv("SIGNULL_BIG_EQUITY_BUFFER", "1.25")),
            strategy_risk_pct=float(os.getenv("SIGNULL_RISK_PCT", "0.10")),
            strategy_id=strategy_id,
            bnb_rpc_url=os.getenv(
                "BNB_RPC_URL", "https://bsc-dataseed.binance.org"
            ),
            wallet_poll_ms=int(os.getenv("WALLET_POLL_MS", "1000")),
            gas_warn_bnb=float(os.getenv("GAS_WARN_BNB", "0.01")),
            fixed_stake_usdc=fixed_stake,
            use_fixed_stake=use_fixed,
            max_chase_attempts=int(os.getenv("MAX_CHASE_ATTEMPTS", "5")),
            chase_interval_sec=float(os.getenv("CHASE_INTERVAL_SEC", "5.0")),
            max_heartbeat_failures=int(os.getenv("MAX_HEARTBEAT_FAILURES", "3")),
            maker_fee_rate=float(os.getenv("MAKER_FEE_RATE", "0.0")),
            taker_fee_rate=float(os.getenv("TAKER_FEE_RATE", "0.02")),
            wallet_balance_stale_sec=float(os.getenv("WALLET_BALANCE_STALE_SEC", "60.0")),
        )

    def strategy_params(self) -> dict:
        if self.custom_strategy_params is not None:
            return dict(self.custom_strategy_params)
        if self.strategy_id == "signull_1_1":
            return {"threshold": self.strategy_threshold, "risk_pct": self.strategy_risk_pct}
        if self.strategy_id == "signull_1_2":
            return {"threshold": self.strategy_threshold, "persist_calibration": self.is_live}
        if self.strategy_id == "signull_1_3":
            return {"threshold": self.strategy_threshold}
        if self.strategy_id == "signull_1_4":
            return {"threshold": self.strategy_threshold, "risk_pct": self.strategy_risk_pct}
        if self.strategy_id == "signull_1_5":
            return {
                "target_delta": float(os.getenv("SIGNULL_TARGET_DELTA", "10.0")),
                "risk_pct": self.strategy_risk_pct,
            }
        if self.strategy_id == "signull_1_6":
            return {
                "threshold": float(os.getenv("SIGNULL_PROB_THRESHOLD", "0.65")),
                "risk_pct": self.strategy_risk_pct,
            }
        if self.strategy_id == "signull_1_7":
            return {
                "risk_pct": self.strategy_risk_pct,
                "asset": self.asset,
                "lookback_minutes": int(os.getenv("SIGNULL_PNR_LOOKBACK_MINUTES", "60")),
                "target_reversal_prob": float(os.getenv("SIGNULL_PNR_RETURN_PROB", "0.35")),
                "min_delta": float(os.getenv("SIGNULL_PNR_MIN_DELTA", "5.0")),
                "max_delta": float(os.getenv("SIGNULL_PNR_MAX_DELTA", "250.0")),
                "range_floor_multiplier": float(os.getenv("SIGNULL_PNR_RANGE_FLOOR_MULT", "0.65")),
                "chop_multiplier": float(os.getenv("SIGNULL_PNR_CHOP_MULT", "0.20")),
                "trend_discount": float(os.getenv("SIGNULL_PNR_TREND_DISCOUNT", "0.30")),
                "threshold_multiplier": float(os.getenv("SIGNULL_PNR_THRESHOLD_MULT", "0.80")),
                "fallback_delta": float(os.getenv("SIGNULL_TARGET_DELTA", "30.0")),
            }
        if self.strategy_id == "signull_1_8":
            return {
                "risk_pct": self.strategy_risk_pct,
                "min_edge": float(os.getenv("SIGNULL_OPEN_EDGE", "0.0")),
                "asset": self.asset,
            }
        if self.strategy_id == "signull_1_9":
            return {
                "risk_pct": self.strategy_risk_pct,
                "entry_seconds": float(os.getenv("SIGNULL_FAV_ENTRY_SECONDS", "180.0")),
                "min_edge": float(os.getenv("SIGNULL_OPEN_EDGE", "0.0")),
                "asset": self.asset,
            }
        if self.strategy_id == "signull_1_10":
            return {
                "target_delta": float(os.getenv("SIGNULL_TARGET_DELTA", "10.0")),
                "risk_pct": self.strategy_risk_pct,
                "vol_lookback_min": int(os.getenv("SIGNULL_REGIME_SHORT_MIN", "30")),
                "baseline_lookback_min": int(os.getenv("SIGNULL_REGIME_BASE_MIN", "180")),
                "vol_multiplier": float(os.getenv("SIGNULL_REGIME_VOL_MULT", "1.2")),
                "min_vol": float(os.getenv("SIGNULL_REGIME_MIN_VOL", "0.0")),
                "invert_when_volatile": os.getenv("SIGNULL_REGIME_INVERT", "true").lower()
                in ("true", "1", "yes"),
                "asset": self.asset,
            }
        if self.strategy_id == "signull_1_11":
            return {
                "min_risk_pct": float(os.getenv("SIGNULL_AI_MIN_RISK_PCT", "0.02")),
                "max_risk_pct": float(os.getenv("SIGNULL_AI_MAX_RISK_PCT", "0.10")),
                "decision_delay_sec": float(os.getenv("SIGNULL_AI_DECISION_DELAY_SEC", "3.0")),
                "entry_window_sec": float(os.getenv("SIGNULL_AI_ENTRY_WINDOW_SEC", "120.0")),
                "btc_lookback_minutes": int(os.getenv("SIGNULL_AI_LOOKBACK_MIN", "90")),
                "temperature": float(os.getenv("SIGNULL_AI_TEMPERATURE", "0.2")),
                "max_tokens": int(os.getenv("SIGNULL_AI_MAX_TOKENS", "700")),
                "asset": self.asset,
            }
        return {
            "threshold": self.strategy_threshold,
            "min_risk_pct": self.strategy_min_risk_pct,
            "max_risk_pct": self.strategy_max_risk_pct,
            "trust_lookback": self.strategy_trust_lookback,
            "btc_align_min": self.strategy_btc_align_min,
            "big_equity_buffer": self.strategy_big_equity_buffer,
        }

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def has_wallet(self) -> bool:
        return bool(self.private_key and self.funder_address)