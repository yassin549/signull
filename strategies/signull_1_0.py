"""
Signull 1.0

Entry: first side (Up or Down) to hit 70% — always fill at exactly 70¢
(like a resting limit order). Side selection is fixed; edge is sizing only.

Size big only when ALL hold:
  1. BTC 1m chart is trending in the same direction as our side
  2. The past few candles did NOT show untrustworthy probability swings
     (a side hitting 70% then falling to 30%, or both sides reaching 70%)
  3. Equity has a buffer vs initial capital (default ≥ 125%) so we do not
     risk 50% until the bankroll has proven itself

Otherwise size small. Never size from entry probability — it is always 70%.
"""

from __future__ import annotations

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal
from src.ml.btc_features import btc_momentum_align, fetch_klines, window_features

STRATEGY_CLASS = "Signull10Strategy"

BTC_LOOKBACK = 60


def candle_is_noisy(
    ticks: list[tuple[int, float, float]],
    *,
    high: float = 0.70,
    low: float = 0.30,
) -> bool:
    """
    True if this candle's Up/Down path looked untrustworthy.

    Untrustworthy = either side hit `high` then later printed `low` or below,
    or both sides reached `high` at some point (70 ↔ 30 style violence).
    """
    if not ticks:
        return False

    saw_up_high = False
    saw_down_high = False
    up_hit_high = False
    down_hit_high = False
    up_min_after_high = 1.0
    down_min_after_high = 1.0

    for _t, up, down in ticks:
        if up >= high:
            saw_up_high = True
            up_hit_high = True
        if down >= high:
            saw_down_high = True
            down_hit_high = True
        if up_hit_high:
            up_min_after_high = min(up_min_after_high, up)
        if down_hit_high:
            down_min_after_high = min(down_min_after_high, down)

    if saw_up_high and saw_down_high:
        return True
    if up_hit_high and up_min_after_high <= low:
        return True
    if down_hit_high and down_min_after_high <= low:
        return True
    return False


class Signull10Strategy(Strategy):
    """
    Signull 1.0 — buy whichever side hits 70% first at exactly 70¢.
    Go big only when BTC is aligned, recent candles were trustworthy,
    and equity has reached the bankroll buffer (default 125% of initial).
    """

    meta = StrategyMeta(
        id="signull_1_0",
        name="Signull 1.0",
        description=(
            "Signull 1.0: limit-style entry — first of Up/Down to 70%, always "
            "filled at 70¢. Binary sizing (small vs big % of initial capital). "
            "Go big only when BTC is trending with our side, recent candles "
            "were trustworthy (no 70%↔30% violence), AND equity is at least "
            "125% of initial (buffer before 50% risk). Otherwise go small."
        ),
        default_params={
            "threshold": 0.70,
            "fill_price": 0.70,
            "min_risk_pct": 0.05,  # small
            "max_risk_pct": 0.50,  # big
            "trust_lookback": 3,  # past N candles for trust
            "noise_high": 0.70,
            "noise_low": 0.30,
            "btc_align_min": 0.55,  # need clear trend with our side
            "btc_lookback": BTC_LOOKBACK,
            # No big size until equity / initial >= this (let stats work first)
            "big_equity_buffer": 1.25,
        },
    )

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self._klines: list | None = None
        # slug -> noisy flag for fully observed candles
        self._noisy_by_slug: dict[str, bool] = {}
        # chronological candle slugs (backtest order)
        self._slug_order: list[str] = []
        self._slug_to_idx: dict[str, int] = {}
        self._last_size_label: str = "small"
        self._last_reason_extra: str = ""

    def prepare_backtest(self, candles) -> None:
        """Precompute per-candle noise flags and preload BTC klines."""
        self._noisy_by_slug.clear()
        self._slug_order = []
        self._slug_to_idx = {}

        if not candles:
            self._klines = None
            return

        high = float(self.params["noise_high"])
        low = float(self.params["noise_low"])

        for i, c in enumerate(candles):
            self._slug_order.append(c.slug)
            self._slug_to_idx[c.slug] = i
            self._noisy_by_slug[c.slug] = candle_is_noisy(
                c.ticks, high=high, low=low
            )

        btc_lb = int(self.params.get("btc_lookback", BTC_LOOKBACK))
        t_min = candles[0].start_ts - btc_lb * 60 - 120
        t_max = candles[-1].end_ts + 60
        self._klines = fetch_klines(t_min, t_max)

    def _recent_trustworthy(self, candle: CandleContext) -> tuple[bool, int, int]:
        """
        Check past `trust_lookback` candles (before current) for noise.

        Returns (trustworthy, noisy_count, looked_at).
        With no history (start of series / live cold start), treat as
        trustworthy=False so we default small until we have evidence.
        """
        lookback = int(self.params["trust_lookback"])
        idx = self._slug_to_idx.get(candle.slug)

        if idx is None:
            # Live / unknown candle: no prior path stats → don't go big
            return False, 0, 0

        start = max(0, idx - lookback)
        prior_slugs = self._slug_order[start:idx]
        if not prior_slugs:
            return False, 0, 0

        noisy = sum(1 for s in prior_slugs if self._noisy_by_slug.get(s, False))
        # Trustworthy only if NONE of the past few candles were noisy
        return noisy == 0, noisy, len(prior_slugs)

    def _btc_aligned(self, entry_ts: int, side: str) -> tuple[bool, float]:
        btc_lb = int(self.params.get("btc_lookback", BTC_LOOKBACK))
        if self._klines is None:
            self._klines = fetch_klines(entry_ts - btc_lb * 60 - 120, entry_ts + 60)

        feats = window_features(self._klines, entry_ts, lookback=btc_lb)
        if feats is None:
            return False, 0.5

        align = btc_momentum_align(feats, side)
        min_align = float(self.params["btc_align_min"])
        return align >= min_align, align

    def evaluate(
        self,
        tick: TickContext,
        candle: CandleContext,
        *,
        entered: bool,
    ) -> TradeSignal | None:
        if entered:
            return None

        threshold = float(self.params["threshold"])
        fill = float(self.params["fill_price"])

        # First side to print >= 70% — limit fill at exactly 70¢
        if tick.up >= threshold:
            return TradeSignal(
                side="up",
                price=fill,
                reason=f"Up first to {threshold:.0%} (limit @{fill:.0%})",
            )

        if tick.down >= threshold:
            return TradeSignal(
                side="down",
                price=fill,
                reason=f"Down first to {threshold:.0%} (limit @{fill:.0%})",
            )

        return None

    def position_risk_fraction(
        self,
        signal: TradeSignal,
        tick: TickContext,
        candle: CandleContext,
    ) -> float:
        lo = float(self.params["min_risk_pct"])
        hi = float(self.params["max_risk_pct"])

        trustworthy, noisy_n, looked = self._recent_trustworthy(candle)
        btc_ok, align = self._btc_aligned(tick.t, signal.side)

        initial = self._initial_capital if self._initial_capital > 0 else 1.0
        equity_ratio = self._equity / initial
        buffer_min = float(self.params.get("big_equity_buffer", 1.25))
        buffer_ok = equity_ratio >= buffer_min

        go_big = trustworthy and btc_ok and buffer_ok

        if go_big:
            self._last_size_label = "big"
            detail = (
                f"trust ok (0/{looked} noisy) · BTC align {align:.2f} · "
                f"equity {equity_ratio:.0%}≥{buffer_min:.0%}"
            )
        else:
            reasons: list[str] = []
            if not trustworthy:
                if looked == 0:
                    reasons.append("no trust history")
                else:
                    reasons.append(f"trust weak ({noisy_n}/{looked} noisy)")
            if not btc_ok:
                reasons.append(f"BTC not aligned ({align:.2f})")
            if not buffer_ok:
                reasons.append(
                    f"buffer {equity_ratio:.0%}<{buffer_min:.0%} initial"
                )
            self._last_size_label = "small"
            detail = " · ".join(reasons) if reasons else "small"

        # Annotate the entry reason with sizing evidence (engine appends size label)
        signal.reason = f"{signal.reason} · {detail}"
        return hi if go_big else lo

    def size_label(self, risk_frac: float) -> str:
        return self._last_size_label
