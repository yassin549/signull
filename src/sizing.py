"""Stake sizing and fill accounting helpers (paper + live)."""

from __future__ import annotations


def compute_stake(
    risk_frac: float,
    initial_capital: float,
    equity: float,
    *,
    wallet_balance: float | None = None,
    is_live: bool = False,
    min_stake: float = 0.01,
) -> float:
    """
    Stake = risk_frac * initial, capped by current equity.

    Live mode additionally caps by wallet collateral when known.
    """
    if risk_frac <= 0 or initial_capital <= 0:
        return 0.0
    stake = min(float(initial_capital) * float(risk_frac), float(equity))
    if is_live and wallet_balance is not None:
        stake = min(stake, max(0.0, float(wallet_balance)))
    if stake < min_stake:
        return 0.0
    return stake


def partial_fill_stake(filled_shares: float, entry_price: float) -> float:
    """USDC notional that actually filled: shares * limit price."""
    if filled_shares <= 0 or entry_price <= 0:
        return 0.0
    return float(filled_shares) * float(entry_price)


def scale_pending_for_fill(
    requested_stake: float,
    requested_shares: float,
    filled_shares: float,
    entry_price: float,
) -> tuple[float, float]:
    """
    Return (effective_stake, effective_shares) after a (partial) fill.

    Uses matched share count when available; falls back to requested stake.
    """
    if filled_shares > 0 and entry_price > 0:
        shares = float(filled_shares)
        stake = partial_fill_stake(shares, entry_price)
        return stake, shares
    if requested_shares > 0:
        return float(requested_stake), float(requested_shares)
    if entry_price > 0 and requested_stake > 0:
        return float(requested_stake), float(requested_stake) / float(entry_price)
    return 0.0, 0.0
