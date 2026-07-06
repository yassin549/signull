"""Trading signal logic for 5M Up/Down candles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .markets import CandleMarket


class Side(Enum):
    UP = "up"
    DOWN = "down"
    HOLD = "hold"


@dataclass
class Signal:
    side: Side
    token_id: str
    price: float
    reason: str


def momentum_signal(
    market: CandleMarket,
    up_mid: float,
    down_mid: float,
    max_entry_price: float,
    min_seconds_left: float = 30.0,
) -> Signal:
    """
    Basic momentum strategy (placeholder — customize later).

    Rules:
    - Don't trade in the last 30 seconds (resolution risk).
    - Buy Up if Up is cheap (< max_entry_price) and market leans Up.
    - Buy Down if Down is cheap and market leans Down.
    - Otherwise hold.
    """
    if market.seconds_to_close < min_seconds_left:
        return Signal(Side.HOLD, "", 0.0, "too close to resolution")

    if not market.accepting_orders:
        return Signal(Side.HOLD, "", 0.0, "market not accepting orders")

    lean = up_mid - down_mid

    if lean > 0.05 and up_mid <= max_entry_price:
        return Signal(
            Side.UP,
            market.up_token_id,
            up_mid,
            f"Up underpriced vs Down (up={up_mid:.3f}, down={down_mid:.3f})",
        )

    if lean < -0.05 and down_mid <= max_entry_price:
        return Signal(
            Side.DOWN,
            market.down_token_id,
            down_mid,
            f"Down underpriced vs Up (up={up_mid:.3f}, down={down_mid:.3f})",
        )

    return Signal(
        Side.HOLD,
        "",
        0.0,
        f"no edge (up={up_mid:.3f}, down={down_mid:.3f})",
    )