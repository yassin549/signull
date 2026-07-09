"""Mine historical 70% entries for hardcoded big/small sizing rules."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.backtest.data import fetch_candles
from src.backtest.engine import _settle_trade
from src.ml.btc_features import btc_momentum_align, fetch_klines, window_features

THRESHOLD = 0.70
LOOKBACK = 60
MIN_RISK = 0.05
MAX_RISK = 0.50
INITIAL = 100.0


@dataclass
class TradeRow:
    entry_price: float
    side: str
    won: bool
    pnl_at_10pct: float
    seconds_in: float
    btc_align: float
    btc_vol: float
    btc_signed_ret: float
    equity_ratio: float
    drawdown: float
    losses_streak: int
    wins_recent: int


def _find_entry(candle, threshold=THRESHOLD):
    for tick_t, up_p, down_p in candle.ticks:
        if up_p >= threshold:
            return tick_t, "up", up_p, max(0.0, tick_t - candle.start_ts)
        if down_p >= threshold:
            return tick_t, "down", down_p, max(0.0, tick_t - candle.start_ts)
    return None


def build_rows(candles, klines) -> list[TradeRow]:
    capital = INITIAL
    peak = capital
    losses_streak = 0
    recent: list[bool] = []
    rows: list[TradeRow] = []

    for candle in candles:
        entry = _find_entry(candle)
        if entry is None:
            continue

        tick_t, side, price, seconds_in = entry
        btc = window_features(klines, tick_t, lookback=LOOKBACK)
        if btc is None:
            continue

        dd = (peak - capital) / peak if peak > 0 else 0.0
        signed_ret = float(np.sum(btc[:, 0])) * (1.0 if side == "up" else -1.0)
        won = side == candle.winner
        stake = INITIAL * 0.10
        pnl = _settle_trade(stake, max(0.01, min(0.99, price)), won)

        rows.append(TradeRow(
            entry_price=price,
            side=side,
            won=won,
            pnl_at_10pct=pnl,
            seconds_in=seconds_in,
            btc_align=btc_momentum_align(btc, side),
            btc_vol=float(np.std(btc[:, 0])),
            btc_signed_ret=signed_ret,
            equity_ratio=capital / INITIAL,
            drawdown=dd,
            losses_streak=losses_streak,
            wins_recent=sum(recent),
        ))

        capital += pnl
        peak = max(peak, capital)
        recent.append(won)
        if len(recent) > 10:
            recent.pop(0)
        losses_streak = 0 if won else losses_streak + 1

    return rows


def wr(rows: list[TradeRow]) -> float:
    return sum(1 for r in rows if r.won) / len(rows) if rows else 0.0


def sim_pnl(rows: list[TradeRow], risk_fn) -> float:
    capital = INITIAL
    for r in rows:
        risk = risk_fn(r)
        stake = min(INITIAL * risk, capital)
        if stake <= 0:
            break
        capital += _settle_trade(stake, max(0.01, min(0.99, r.entry_price)), r.won)
    return capital


def bucket_report(rows: list[TradeRow], name: str, values: list[float], edges: list[float]) -> None:
    print(f"\n=== {name} ===")
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        sub = [r for r, v in zip(rows, values) if lo <= v < hi]
        if not sub:
            continue
        label = f"[{lo:.2f}, {hi:.2f})" if hi < 999 else f">={lo:.2f}"
        avg_p = np.mean([r.entry_price for r in sub])
        print(f"  {label:14s} n={len(sub):3d}  WR={wr(sub):5.1%}  avg_entry={avg_p:.3f}")


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    candles = fetch_candles("btc", count, use_cache=True)
    t_min = candles[0].start_ts - LOOKBACK * 60 - 120
    t_max = candles[-1].end_ts + 60
    klines = fetch_klines(t_min, t_max)
    rows = build_rows(candles, klines)

    print(f"Trades: {len(rows)}  overall WR: {wr(rows):.1%}")

    bucket_report(rows, "Entry probability", [r.entry_price for r in rows],
                  [0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.01])
    bucket_report(rows, "BTC alignment", [r.btc_align for r in rows],
                  [0.0, 0.40, 0.50, 0.55, 0.60, 0.70, 1.01])
    bucket_report(rows, "BTC signed 60m ret", [r.btc_signed_ret for r in rows],
                  [-0.01, -0.003, 0.0, 0.003, 0.01, 999])
    bucket_report(rows, "BTC vol (std lr)", [r.btc_vol for r in rows],
                  [0.0, 0.0008, 0.0012, 0.0016, 0.0025, 999])
    bucket_report(rows, "Seconds into candle", [r.seconds_in for r in rows],
                  [0, 60, 120, 180, 300, 600, 99999])
    bucket_report(rows, "Drawdown at entry", [r.drawdown for r in rows],
                  [0.0, 0.10, 0.20, 0.35, 0.50, 999])
    bucket_report(rows, "Loss streak", [float(r.losses_streak) for r in rows],
                  [0, 1, 2, 3, 5, 999])

    # Grid search simple rule combos
    print("\n=== Rule grid (end capital, $100 start) ===")

    def prob_only(r: TradeRow) -> float:
        t = max(0.0, min(1.0, (r.entry_price - 0.70) / 0.22))
        return MIN_RISK + t * (MAX_RISK - MIN_RISK)

    def prob_btc(r: TradeRow) -> float:
        base = prob_only(r)
        if r.btc_align >= 0.55 and r.btc_vol < 0.0016:
            return min(MAX_RISK, base * 1.15)
        if r.btc_align < 0.45 or r.btc_vol >= 0.0020:
            return max(MIN_RISK, base * 0.65)
        return base

    def prob_dd(r: TradeRow) -> float:
        base = prob_only(r)
        if r.drawdown >= 0.25 or r.losses_streak >= 2:
            return max(MIN_RISK, MIN_RISK + (base - MIN_RISK) * 0.4)
        return base

    def combo(r: TradeRow) -> float:
        # Data-driven: prob primary, BTC confirms, drawdown/streak dampen
        t = max(0.0, min(1.0, (r.entry_price - 0.70) / 0.20))
        score = t
        if r.btc_align >= 0.55:
            score += 0.15
        elif r.btc_align < 0.45:
            score -= 0.20
        if r.btc_vol >= 0.0020:
            score -= 0.15
        if r.drawdown >= 0.20:
            score -= 0.20
        if r.losses_streak >= 2:
            score -= 0.15
        if r.seconds_in < 60:
            score += 0.05
        score = max(0.0, min(1.0, score))
        return MIN_RISK + score * (MAX_RISK - MIN_RISK)

    rules = [
        ("flat 10%", lambda r: 0.10),
        ("flat 25%", lambda r: 0.25),
        ("prob linear", prob_only),
        ("prob + btc", prob_btc),
        ("prob + drawdown", prob_dd),
        ("combo rules", combo),
    ]
    for name, fn in rules:
        end = sim_pnl(rows, fn)
        print(f"  {name:18s}  ${end:7.2f}  ({(end/INITIAL-1)*100:+.1f}%)")

    # Best threshold hints for combo
    print("\n=== Combo rule: WR by score bucket ===")
    scores = []
    for r in rows:
        t = max(0.0, min(1.0, (r.entry_price - 0.70) / 0.20))
        score = t
        if r.btc_align >= 0.55:
            score += 0.15
        elif r.btc_align < 0.45:
            score -= 0.20
        if r.btc_vol >= 0.0020:
            score -= 0.15
        if r.drawdown >= 0.20:
            score -= 0.20
        if r.losses_streak >= 2:
            score -= 0.15
        if r.seconds_in < 60:
            score += 0.05
        scores.append(max(0.0, min(1.0, score)))

    bucket_report(rows, "Combo score", scores, [0.0, 0.25, 0.45, 0.60, 0.75, 1.01])


if __name__ == "__main__":
    main()