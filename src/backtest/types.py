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
    ticks: list[tuple[int, float, float]]  # (t, up, down)

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
        }