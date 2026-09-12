"""Infer whether a live GTC limit buy would fill from historical last-trade prints.

Live places a GTC limit at the signal price and cancels leftovers at candle
close. Cached candles only have last-trade / mid prices — not bid/ask depth —
so this is a reconstruction, not a book replay:

- The signal tick is not our fill; that print happened before the order
  would be on the book.
- A later print at or below the limit means a seller hit that price and
  the resting bid would fill.
- If the last trade at submit is already strictly below the limit, the
  bid is through the market and we treat it as an immediate take.
- Orders in the last LATE_CANDLE_SEC of the market are treated as
  rejected (the CLOB often stops accepting near close).
- No later prints and not through at submit → unfilled; we cannot prove
  a fill without depth.
"""

from __future__ import annotations

from dataclasses import dataclass

# CLOB often rejects new orders in the last seconds of a 5-minute market.
LATE_CANDLE_SEC = 10.0

REASON_FILLED = "filled"
REASON_CROSSED = "crossed_at_submit"
REASON_NO_RETRACE = "no_retrace"
REASON_LATE = "late_candle"
REASON_NO_TICKS = "no_subsequent_ticks"

LIVE_FILL_LABELS = {
    REASON_FILLED: "Resting bid was hit by a later last-trade print at or through the limit.",
    REASON_CROSSED: "Last trade at submit was already through the limit, so a GTC bid would take.",
    REASON_NO_RETRACE: "Price never traded back to the limit before candle close — GTC would cancel unfilled.",
    REASON_LATE: "Signal fired too close to candle close; live orders are often rejected then.",
    REASON_NO_TICKS: "No later prints after submit, so a fill cannot be confirmed without book depth.",
}


@dataclass(frozen=True)
class LiveFill:
    filled: bool
    reason: str
    fill_ts: int | None = None
    worst_price: float | None = None


def _side_price(up: float, down: float, side: str) -> float:
    return float(up) if side == "up" else float(down)


def simulate_live_limit_buy(
    *,
    side: str,
    limit_price: float,
    entry_ts: int,
    seconds_to_close: float,
    ticks: list[tuple[int, float, float]],
    end_ts: int | None = None,
    late_candle_sec: float = LATE_CANDLE_SEC,
) -> LiveFill:
    """Return whether a live GTC buy at `limit_price` would fill in this candle."""
    limit_price = float(limit_price)
    if limit_price <= 0:
        return LiveFill(False, REASON_NO_RETRACE)

    if seconds_to_close < late_candle_sec:
        return LiveFill(False, REASON_LATE)

    signal_price: float | None = None
    later: list[tuple[int, float]] = []
    for tick_t, up_p, down_p in ticks:
        if end_ts is not None and tick_t > end_ts:
            continue
        px = _side_price(up_p, down_p, side)
        if tick_t < entry_ts:
            continue
        if tick_t == entry_ts:
            signal_price = px
            continue
        later.append((tick_t, px))

    if signal_price is not None and signal_price < limit_price:
        return LiveFill(True, REASON_CROSSED, fill_ts=entry_ts, worst_price=signal_price)

    if later:
        worst = min(px for _, px in later)
        for ts, px in later:
            if px <= limit_price:
                return LiveFill(True, REASON_FILLED, fill_ts=ts, worst_price=worst)
        return LiveFill(False, REASON_NO_RETRACE, worst_price=worst)

    return LiveFill(False, REASON_NO_TICKS, worst_price=signal_price)
