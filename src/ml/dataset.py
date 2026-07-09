"""Build TCN training samples from historical candles + BTC + equity path."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

from src.backtest.data import fetch_candles
from src.backtest.engine import _settle_trade
from src.backtest.types import CandleDataset
from src.ml.btc_features import btc_momentum_align, fetch_klines, window_features

from strategies.base import CandleContext, TickContext

logger = logging.getLogger(__name__)

THRESHOLD = 0.70
MIN_RISK = 0.05
MAX_RISK = 0.50
LOOKBACK = 60


EQUITY_DIM = 7


@dataclass
class SizerSample:
    btc_feats: np.ndarray  # (LOOKBACK, 3)
    equity_feats: np.ndarray  # (EQUITY_DIM,)
    label_risk: float
    side: str
    won: bool
    entry_ts: int


def _signed_btc_momentum(btc_feats: np.ndarray, side: str) -> float:
    ret = float(np.sum(btc_feats[:, 0])) if btc_feats is not None else 0.0
    return ret if side == "up" else -ret


def _equity_features(
    equity: float,
    initial: float,
    peak: float,
    wins_recent: int,
    losses_streak: int,
    *,
    equity_momentum: float = 0.0,
    btc_feats: np.ndarray | None = None,
    side: str = "up",
) -> np.ndarray:
    dd = (peak - equity) / peak if peak > 0 else 0.0
    signed_mom = _signed_btc_momentum(btc_feats, side) if btc_feats is not None else 0.0
    return np.array([
        equity / initial if initial > 0 else 1.0,
        1.0 - min(1.0, dd),
        wins_recent / 10.0,
        min(losses_streak, 5) / 5.0,
        np.log(max(equity, 1.0) / initial) if initial > 0 else 0.0,
        float(np.tanh(equity_momentum / initial)) if initial > 0 else 0.0,
        float(np.tanh(signed_mom * 40.0)),
    ], dtype=np.float32)


def _ideal_risk_label(
    *,
    equity: float,
    initial: float,
    peak: float,
    losses_streak: int,
    side: str,
    btc_feats: np.ndarray,
) -> float:
    """
    Entry-time teacher: size up when equity is healthy, BTC aligns with our side,
    and vol is tame; size down in drawdown, misalignment, or losing streaks.

    Uses only features available at inference (no trade outcome).
    """
    dd = (peak - equity) / peak if peak > 0 else 0.0
    afford = min(1.0, equity / initial) * (1.0 - min(1.0, dd)) if initial > 0 else 0.5
    align = btc_momentum_align(btc_feats, side)
    vol = float(np.std(btc_feats[:, 0])) if btc_feats is not None else 0.0
    vol_penalty = float(np.clip(1.0 - vol * 80.0, 0.5, 1.0))
    streak_penalty = float(np.clip(1.0 - min(losses_streak, 3) * 0.15, 0.5, 1.0))

    # Stretch BTC alignment across the full 5–50% band; equity/vol/streak modulate.
    t = float(np.clip((align - 0.30) / 0.50, 0.0, 1.0))
    mod = (0.60 + 0.40 * afford) * vol_penalty * streak_penalty
    return MIN_RISK + (MAX_RISK - MIN_RISK) * float(np.clip(t * mod, 0.0, 1.0))


def _find_entry(candle: CandleDataset, threshold: float = THRESHOLD):
    ctx = CandleContext(
        slug=candle.slug,
        title=candle.title,
        start_ts=candle.start_ts,
        end_ts=candle.end_ts,
        winner=candle.winner,
    )
    for tick_t, up_p, down_p in candle.ticks:
        tick = TickContext(
            t=tick_t,
            up=up_p,
            down=down_p,
            seconds_into_candle=max(0.0, tick_t - candle.start_ts),
            seconds_to_close=max(0.0, candle.end_ts - tick_t),
        )
        if up_p >= threshold:
            return tick, "up", up_p, ctx
        if down_p >= threshold:
            return tick, "down", down_p, ctx
    return None


def build_samples(
    candles: list[CandleDataset],
    *,
    initial_capital: float = 100.0,
) -> list[SizerSample]:
    """Walk candles chronologically; build equity path + training rows."""
    if not candles:
        return []

    t_min = candles[0].start_ts - LOOKBACK * 60 - 120
    t_max = candles[-1].end_ts + 60
    klines = fetch_klines(t_min, t_max)

    capital = float(initial_capital)
    peak = capital
    wins_recent = 0
    losses_streak = 0
    recent_outcomes: list[bool] = []
    equity_hist: list[float] = [capital]

    samples: list[SizerSample] = []

    for candle in candles:
        entry = _find_entry(candle)
        if entry is None:
            continue

        tick, side, entry_price, _ctx = entry
        btc_feats = window_features(klines, tick.t, lookback=LOOKBACK)
        if btc_feats is None:
            continue

        mom = capital - equity_hist[-5] if len(equity_hist) >= 5 else capital - initial_capital
        eq_feats = _equity_features(
            capital,
            initial_capital,
            peak,
            wins_recent,
            losses_streak,
            equity_momentum=mom,
            btc_feats=btc_feats,
            side=side,
        )

        label = _ideal_risk_label(
            equity=capital,
            initial=initial_capital,
            peak=peak,
            losses_streak=losses_streak,
            side=side,
            btc_feats=btc_feats,
        )
        stake = min(initial_capital * MIN_RISK, capital)
        entry_price = max(0.01, min(0.99, entry_price))
        won = side == candle.winner
        pnl = _settle_trade(stake, entry_price, won)

        samples.append(SizerSample(
            btc_feats=btc_feats,
            equity_feats=eq_feats,
            label_risk=label,
            side=side,
            won=won,
            entry_ts=tick.t,
        ))

        capital += pnl
        equity_hist.append(capital)
        peak = max(peak, capital)
        recent_outcomes.append(won)
        if len(recent_outcomes) > 10:
            recent_outcomes.pop(0)
        wins_recent = sum(recent_outcomes)
        if won:
            losses_streak = 0
        else:
            losses_streak += 1

    return samples


def load_training_data(
    asset: str = "btc",
    candle_count: int = 500,
) -> list[SizerSample]:
    logger.info("Loading %s candles for TCN dataset…", candle_count)
    candles = fetch_candles(asset, candle_count, use_cache=True, max_workers=10)
    logger.info("Resolved %s candles", len(candles))
    samples = build_samples(candles)
    logger.info("Built %s training samples", len(samples))
    return samples