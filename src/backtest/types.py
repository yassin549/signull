"""Shared datatypes for backtesting."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PricePoint:
    t: int
    p: float


@dataclass
class CandleDataset:
    slug: str
    title: str
    start_ts: int
    end_ts: int
    winner: str  # "up" | "down"
    up_token_id: str
    down_token_id: str
    ticks: list[tuple[int, float, float]]  # (t, up, down) real Predict.fun trade prints
    market_id: int = 0
    fee_rate_bps: int = 200
    # True when ticks were reconstructed from the venue's low-resolution chance
    # series instead of the real trade tape.  Fills on such candles are flagged.
    synthetic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "title": self.title,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "winner": self.winner,
            "up_token_id": self.up_token_id,
            "down_token_id": self.down_token_id,
            "ticks": [{"t": t, "up": u, "down": d} for t, u, d in self.ticks],
            "market_id": self.market_id,
            "fee_rate_bps": self.fee_rate_bps,
            "synthetic": self.synthetic,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandleDataset:
        ticks = [(int(x["t"]), float(x["up"]), float(x["down"])) for x in data.get("ticks", [])]
        return cls(
            slug=data["slug"],
            title=data["title"],
            start_ts=int(data["start_ts"]),
            end_ts=int(data["end_ts"]),
            winner=data["winner"],
            up_token_id=data["up_token_id"],
            down_token_id=data["down_token_id"],
            ticks=ticks,
            market_id=int(data.get("market_id", 0)),
            fee_rate_bps=int(data.get("fee_rate_bps", 200)),
            synthetic=bool(data.get("synthetic", False)),
        )


@dataclass
class TradeRecord:
    candle_slug: str
    candle_title: str
    side: str
    entry_price: float
    stake: float
    shares: float
    winner: str
    won: bool
    pnl: float
    equity_after: float
    entry_ts: int
    reason: str
    risk_pct: float = 0.0
    size_label: str = ""
    entry_fee: float = 0.0
    sim_entry_prob: float | None = None
    sim_lifetime_probs: list[dict[str, Any]] | None = None
    # Whether the live-style limit order actually filled against the tape.
    filled: bool = True
    fill_reason: str = "filled"
    limit_price: float = 0.0
    synthetic: bool = False


@dataclass
class BacktestResult:
    strategy_id: str
    strategy_name: str
    params: dict[str, Any]
    initial_capital: float
    ending_capital: float
    total_return_pct: float
    candles_loaded: int
    candles_traded: int
    wins: int
    losses: int
    win_rate: float
    max_drawdown_pct: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[dict[str, float]] = field(default_factory=list)
    elapsed_ms: float = 0.0
    # Fill accounting: signals that could not be filled by the real tape.
    fills: int = 0
    unfilled: int = 0
    fill_rate: float = 100.0
    fetch_failures: int = 0
    synthetic_candles: int = 0
    data_source: str = "predict.fun"

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "params": self.params,
            "initial_capital": self.initial_capital,
            "ending_capital": self.ending_capital,
            "total_return_pct": self.total_return_pct,
            "candles_loaded": self.candles_loaded,
            "candles_traded": self.candles_traded,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "max_drawdown_pct": self.max_drawdown_pct,
            "profit_factor": self.profit_factor,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "trades": [t.__dict__ for t in self.trades],
            "equity_curve": self.equity_curve,
            "elapsed_ms": self.elapsed_ms,
            "fills": self.fills,
            "unfilled": self.unfilled,
            "fill_rate": self.fill_rate,
            "fetch_failures": self.fetch_failures,
            "synthetic_candles": self.synthetic_candles,
            "data_source": self.data_source,
        }
