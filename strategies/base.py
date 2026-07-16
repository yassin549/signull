"""Strategy interface used by the backtest engine and live bot."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StrategyMeta:
    id: str
    name: str
    description: str
    default_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class TickContext:
    """Single price observation inside a candle."""

    t: int
    up: float
    down: float
    seconds_into_candle: float
    seconds_to_close: float


@dataclass
class CandleContext:
    """Resolved candle metadata available to strategies."""

    slug: str
    title: str
    start_ts: int
    end_ts: int
    winner: str  # "up" | "down"


@dataclass
class TradeSignal:
    side: str  # "up" | "down"
    price: float
    reason: str
    # The CLOB's taker-fee rate used for this entry. Most strategies remain
    # fee-free until they explicitly opt in; crypto Signull entries set this.
    taker_fee_rate: float = 0.0


class Strategy(ABC):
    """Subclass and register via STRATEGY_CLASS in a strategies/*.py module."""

    meta: StrategyMeta

    def __init__(self, params: dict[str, Any] | None = None):
        merged = dict(self.meta.default_params)
        if params:
            merged.update(params)
        self.params = merged
        self._equity: float = 100.0
        self._initial_capital: float = 100.0
        self._peak_equity: float = 100.0
        self._wins_recent: int = 0
        self._wins_streak: int = 0
        self._losses_streak: int = 0
        self._equity_momentum: float = 0.0

    def on_account_update(
        self,
        equity: float,
        initial_capital: float,
        peak_equity: float,
        *,
        wins_recent: int = 0,
        wins_streak: int = 0,
        losses_streak: int = 0,
        equity_momentum: float = 0.0,
    ) -> None:
        """Called by the backtest engine before each candle."""
        self._equity = equity
        self._initial_capital = initial_capital
        self._peak_equity = peak_equity
        self._wins_recent = wins_recent
        self._wins_streak = wins_streak
        self._losses_streak = losses_streak
        self._equity_momentum = equity_momentum

    def on_trade_settled(self, won: bool) -> None:
        """Receive the outcome of a trade placed by this strategy.

        The default is intentionally a no-op. Adaptive strategies can use this
        hook to update online calibration without the engine knowing their
        model details.
        """

    def on_signal_resolved(self, won: bool, *, traded: bool) -> None:
        """Receive the eventual outcome of every eligible backtest signal.

        `traded` distinguishes an executed trade from a signal that was
        deliberately declined by position sizing.  Adaptive strategies can
        keep learning after a zero-risk decision instead of freezing forever.
        """
        del won, traded

    def register_closed_candle(
        self,
        slug: str,
        ticks: list[tuple[int, float, float]],
    ) -> bool:
        """Receive a completed live candle and return whether its path was noisy.

        The live bot calls this for every candle, including when no trade was
        entered.  Most strategies do not use intrabar-path trust, so their
        default is a clean no-op.  Strategies that do use it (such as
        Signull 1.0) override this hook.
        """
        del slug, ticks
        return False

    @abstractmethod
    def evaluate(self, tick: TickContext, candle: CandleContext, *, entered: bool) -> TradeSignal | None:
        """
        Return a trade signal on this tick, or None to keep waiting.
        `entered` is True once a trade was already taken this candle.
        """

    def position_risk_fraction(
        self,
        signal: TradeSignal,
        tick: TickContext,
        candle: CandleContext,
    ) -> float:
        """
        Fraction of *initial* capital to stake on this trade (e.g. 0.05 = 5%).

        Override in subclasses for dynamic small/big sizing.
        """
        return float(self.params.get("risk_pct", 0.10))
