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
    fixed_stake: float | None = None,
) -> float:
    """
    Stake = fixed_stake if provided, else risk_frac * initial, capped by current equity.

    Live mode additionally caps by wallet collateral when known.
    """
    if fixed_stake is not None and fixed_stake > 0:
        stake = min(float(fixed_stake), float(equity))
    else:
        if risk_frac <= 0 or initial_capital <= 0:
            return 0.0
        stake = min(float(initial_capital) * float(risk_frac), float(equity))
    if is_live and wallet_balance is not None:
        stake = min(stake, max(0.0, float(wallet_balance)))
    if stake < min_stake:
        return 0.0
    return stake


def estimate_maker_fee(stake: float, entry_price: float, fee_rate: float) -> float:
    """
    Estimate maker fee (rebate) for a limit order resting on the book.

    Polymarket CLOB: maker orders receive a rebate (negative fee).
    Fee = -stake * |fee_rate| * price * (1 - price) for binary tokens.
    """
    if stake <= 0 or not 0 < entry_price < 1:
        return 0.0
    # Maker rebate: negative fee credited to account
    # Binary token: shares = stake/price, fee = shares * |rate| * price * (1-price)
    # Simplified: -stake * |rate| * (1 - price)
    return -float(stake) * abs(float(fee_rate)) * (1.0 - float(entry_price))


def estimate_taker_fee(stake: float, entry_price: float, fee_rate: float) -> float:
    """
    Estimate taker fee for a market/aggressive limit order crossing the spread.

    Polymarket CLOB: taker orders pay a fee.
    Fee = stake * fee_rate * (1 - price) for binary tokens.
    """
    if stake <= 0 or not 0 < entry_price < 1 or fee_rate <= 0:
        return 0.0
    # Taker fee: shares * rate * price * (1 - price), where stake = shares * price
    # Simplified: stake * rate * (1 - price)
    return float(stake) * float(fee_rate) * (1.0 - float(entry_price))


def cap_stake_for_taker_fee(
    stake: float,
    entry_price: float,
    fee_rate: float,
    available_cash: float,
) -> float:
    """Cap share notional so notional plus the estimated fee fits cash."""
    if stake <= 0 or available_cash <= 0:
        return 0.0
    multiplier = 1.0 + max(0.0, float(fee_rate)) * (1.0 - float(entry_price))
    return min(float(stake), float(available_cash) / multiplier)


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
