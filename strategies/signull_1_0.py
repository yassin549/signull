"""
Signull 1.0

Limit entry at `threshold` — fill only when the market actually trades through
that price. Side selection is fixed; edge is sizing only.

Two regimes (auto from threshold):
  • Underdog  (threshold < 0.50): wait for a side to DROP to ≤ threshold
    e.g. 10¢ — do NOT fire while mids sit at ~50¢.
  • Favorite  (threshold ≥ 0.50): wait for a side to RISE to ≥ threshold
    e.g. 70¢ — classic "first to 70%" entry.

Fill price is always exactly `threshold` (resting limit). We only fire when
the printed mid has crossed the limit, so we never invent a fill at 10¢ while
the book is still 50¢.

Size big only when ALL hold:
  1. BTC 1m chart is trending in the same direction as our side
  2. Recent candles did NOT show untrustworthy path violence
  3. Equity ≥ buffer vs initial (default 125%)
"""

from __future__ import annotations

import threading
import time

from strategies.base import CandleContext, Strategy, StrategyMeta, TickContext, TradeSignal
from src.ml.btc_features import btc_momentum_align, fetch_klines, window_features

STRATEGY_CLASS = "Signull10Strategy"

BTC_LOOKBACK = 60


def is_underdog_threshold(threshold: float) -> bool:
    """Cheap-side limit if below 50¢; favorite limit at/above 50¢."""
    return float(threshold) < 0.50


def candle_is_noisy(
    ticks: list[tuple[int, float, float]],
    *,
    threshold: float,
) -> bool:
    """
    Path looked untrustworthy relative to the entry regime.

    Underdog (thr < 0.5): a side printed ≤ thr then later ≥ 1-thr
    (cheap → expensive flip), or both sides were ≤ thr at some point.

    Favorite (thr ≥ 0.5): a side printed ≥ thr then later ≤ 1-thr
    (favorite → dog flip), or both sides hit thr.
    """
    if not ticks:
        return False

    thr = float(threshold)
    comp = max(0.01, min(0.99, 1.0 - thr))
    underdog = is_underdog_threshold(thr)

    if underdog:
        # Cheap prints
        up_hit_cheap = down_hit_cheap = False
        up_flipped = down_flipped = False
        for _t, up, down in ticks:
            if up <= thr:
                up_hit_cheap = True
            if down <= thr:
                down_hit_cheap = True
            if up_hit_cheap and up >= comp:
                up_flipped = True
            if down_hit_cheap and down >= comp:
                down_flipped = True
        if up_hit_cheap and down_hit_cheap:
            return True
        return up_flipped or down_flipped

    # Favorite regime
    saw_up_hi = saw_down_hi = False
    up_hit_hi = down_hit_hi = False
    up_min_after = down_min_after = 1.0
    for _t, up, down in ticks:
        if up >= thr:
            saw_up_hi = True
            up_hit_hi = True
        if down >= thr:
            saw_down_hi = True
            down_hit_hi = True
        if up_hit_hi:
            up_min_after = min(up_min_after, up)
        if down_hit_hi:
            down_min_after = min(down_min_after, down)

    if saw_up_hi and saw_down_hi:
        return True
    if up_hit_hi and up_min_after <= comp:
        return True
    if down_hit_hi and down_min_after <= comp:
        return True
    return False


def first_limit_hit(
    tick: TickContext,
    threshold: float,
) -> tuple[str, float] | None:
    """
    Return (side, market_print) if a resting limit at `threshold` would fill.

    Underdog: mid ≤ threshold (odds dropped to our bid).
    Favorite: mid ≥ threshold (odds rose to our bid on the favorite).
    If both hit on the same tick, pick the side that is more through the limit.
    """
    thr = float(threshold)
    underdog = is_underdog_threshold(thr)

    if underdog:
        up_hit = tick.up <= thr
        down_hit = tick.down <= thr
        if up_hit and down_hit:
            # More through the limit = cheaper
            if tick.up <= tick.down:
                return "up", tick.up
            return "down", tick.down
        if up_hit:
            return "up", tick.up
        if down_hit:
            return "down", tick.down
        return None

    up_hit = tick.up >= thr
    down_hit = tick.down >= thr
    if up_hit and down_hit:
        # More through = higher favorite
        if tick.up >= tick.down:
            return "up", tick.up
        return "down", tick.down
    if up_hit:
        return "up", tick.up
    if down_hit:
        return "down", tick.down
    return None


class Signull10Strategy(Strategy):
    """
    Signull 1.0 — limit at threshold with correct underdog/favorite crossing.
    """

    meta = StrategyMeta(
        id="signull_1_0",
        name="Signull 1.0",
        description=(
            "Signull 1.0: resting limit at 70¢ (default). Favorite thr (≥50¢): "
            "fill only when a side RISES to ≥ thr. Underdog thr (<50¢): fill only "
            "when a side DROPS to ≤ thr. Binary sizing via spot-chart align + path "
            "trust + equity buffer; 3–4 consecutive wins use 25% risk."
        ),
        default_params={
            "threshold": 0.70,
            "min_risk_pct": 0.05,
            "hot_streak_risk_pct": 0.25,
            "max_risk_pct": 0.50,
            "trust_lookback": 3,
            "btc_align_min": 0.55,
            "btc_lookback": BTC_LOOKBACK,
            "big_equity_buffer": 1.25,
        },
    )

    def __init__(self, params: dict | None = None, *, asset: str = "btc"):
        super().__init__(params)
        self._normalize_params()
        self._asset = str(self.params.get("asset", asset) or "btc").lower()
        self._klines: list | None = None
        self._klines_fetched_at: float = 0.0
        self._klines_lock = threading.Lock()
        self._klines_refreshing = False
        self._noisy_by_slug: dict[str, bool] = {}
        self._slug_order: list[str] = []
        self._slug_to_idx: dict[str, int] = {}
        self._last_size_label: str = "small"

    def _klines_are_fresh_locked(self, now: float | None = None) -> bool:
        """Caller must hold `_klines_lock`. Empty/None is never fresh."""
        t = time.time() if now is None else now
        return bool(self._klines) and (t - self._klines_fetched_at) < 45

    def _store_klines_result_locked(self, rows: list | None) -> None:
        """Caller must hold `_klines_lock`. Only non-empty series is cached."""
        if rows:
            self._klines = rows
            self._klines_fetched_at = time.time()
        else:
            self._klines = None
            self._klines_fetched_at = 0.0

    def _fetch_klines_rows(self, around_ts: int | None) -> list:
        btc_lb = int(self.params.get("btc_lookback", BTC_LOOKBACK))
        end = int(around_ts if around_ts is not None else time.time())
        start = end - btc_lb * 60 - 180
        return fetch_klines(start, end + 60, asset=self._asset) or []

    def register_closed_candle(
        self,
        slug: str,
        ticks: list[tuple[int, float, float]],
    ) -> bool:
        """Record a fully observed candle for live trust lookback."""
        thr = self._threshold()
        noisy = candle_is_noisy(ticks, threshold=thr)

        if slug in self._slug_to_idx:
            self._noisy_by_slug[slug] = noisy
            return noisy

        idx = len(self._slug_order)
        self._slug_order.append(slug)
        self._slug_to_idx[slug] = idx
        self._noisy_by_slug[slug] = noisy
        max_keep = max(50, int(self.params["trust_lookback"]) * 10)
        if len(self._slug_order) > max_keep:
            drop = len(self._slug_order) - max_keep
            for old in self._slug_order[:drop]:
                self._noisy_by_slug.pop(old, None)
                self._slug_to_idx.pop(old, None)
            self._slug_order = self._slug_order[drop:]
            self._slug_to_idx = {s: i for i, s in enumerate(self._slug_order)}
        return noisy

    def ensure_current_candle(self, slug: str) -> None:
        if slug in self._slug_to_idx:
            return
        idx = len(self._slug_order)
        self._slug_order.append(slug)
        self._slug_to_idx[slug] = idx
        self._noisy_by_slug.setdefault(slug, False)

    def refresh_btc_klines(self, around_ts: int | None = None) -> None:
        """Synchronous kline fetch. Empty/failed results are not cached."""
        with self._klines_lock:
            if self._klines_are_fresh_locked():
                return
            if self._klines_refreshing:
                return
            self._klines_refreshing = True
        try:
            rows = self._fetch_klines_rows(around_ts)
            with self._klines_lock:
                self._store_klines_result_locked(rows)
        finally:
            with self._klines_lock:
                self._klines_refreshing = False

    def schedule_klines_refresh(self, around_ts: int | None = None) -> None:
        """Kick a background kline refresh so the trading tick never blocks on HTTP.

        At most one refresh thread is in flight; empty/failed caches do not
        suppress retries (see refresh_btc_klines / _store_klines_result_locked).
        """
        with self._klines_lock:
            if self._klines_are_fresh_locked():
                return
            if self._klines_refreshing:
                return
            self._klines_refreshing = True

        def _worker() -> None:
            try:
                rows = self._fetch_klines_rows(around_ts)
                with self._klines_lock:
                    self._store_klines_result_locked(rows)
            finally:
                with self._klines_lock:
                    self._klines_refreshing = False

        threading.Thread(
            target=_worker,
            daemon=True,
            name="signull-klines",
        ).start()

    def _normalize_params(self) -> None:
        p = self.params
        try:
            thr = float(p.get("threshold", 0.70))
        except (TypeError, ValueError):
            thr = 0.70
        if thr != thr or thr < 0.01 or thr >= 0.99:
            thr = 0.70
        p["threshold"] = thr
        p.pop("fill_price", None)

        for key, default in (
            ("min_risk_pct", 0.05),
            ("hot_streak_risk_pct", 0.25),
            ("max_risk_pct", 0.50),
            ("btc_align_min", 0.55),
            ("big_equity_buffer", 1.25),
        ):
            try:
                v = float(p.get(key, default))
            except (TypeError, ValueError):
                v = default
            if v != v:
                v = default
            p[key] = v

        try:
            p["trust_lookback"] = max(1, int(float(p.get("trust_lookback", 3))))
        except (TypeError, ValueError):
            p["trust_lookback"] = 3
        try:
            p["btc_lookback"] = max(5, int(float(p.get("btc_lookback", BTC_LOOKBACK))))
        except (TypeError, ValueError):
            p["btc_lookback"] = BTC_LOOKBACK

    def _threshold(self) -> float:
        return float(self.params["threshold"])

    def prepare_backtest(self, candles) -> None:
        self._normalize_params()
        self._noisy_by_slug.clear()
        self._slug_order = []
        self._slug_to_idx = {}

        if not candles:
            self._klines = None
            return

        thr = self._threshold()
        for i, c in enumerate(candles):
            self._slug_order.append(c.slug)
            self._slug_to_idx[c.slug] = i
            self._noisy_by_slug[c.slug] = candle_is_noisy(c.ticks, threshold=thr)

        btc_lb = int(self.params.get("btc_lookback", BTC_LOOKBACK))
        t_min = candles[0].start_ts - btc_lb * 60 - 120
        t_max = candles[-1].end_ts + 60
        rows = fetch_klines(t_min, t_max, asset=self._asset)
        with self._klines_lock:
            self._store_klines_result_locked(rows)
            self._klines_refreshing = False

    def _recent_trustworthy(self, candle: CandleContext) -> tuple[bool, int, int]:
        lookback = int(self.params["trust_lookback"])
        idx = self._slug_to_idx.get(candle.slug)

        if idx is None:
            return False, 0, 0

        start = max(0, idx - lookback)
        prior_slugs = self._slug_order[start:idx]
        if not prior_slugs:
            return False, 0, 0

        noisy = sum(1 for s in prior_slugs if self._noisy_by_slug.get(s, False))
        return noisy == 0, noisy, len(prior_slugs)

    def _btc_aligned(self, entry_ts: int, side: str) -> tuple[bool, float]:
        btc_lb = int(self.params.get("btc_lookback", BTC_LOOKBACK))
        with self._klines_lock:
            klines = list(self._klines) if self._klines else None
        if not klines:
            # Avoid blocking the hot path: schedule refresh; size small until ready.
            self.schedule_klines_refresh(entry_ts)
            return False, 0.5

        feats = window_features(klines, entry_ts, lookback=btc_lb)
        if feats is None:
            # Stale/insufficient series — drop cache so refresh can recover.
            with self._klines_lock:
                self._klines = None
                self._klines_fetched_at = 0.0
            self.schedule_klines_refresh(entry_ts)
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

        threshold = self._threshold()
        hit = first_limit_hit(tick, threshold)
        if hit is None:
            return None

        side, mkt = hit
        fill = threshold
        underdog = is_underdog_threshold(threshold)
        cmp = "≤" if underdog else "≥"
        regime = "underdog" if underdog else "favorite"

        # Safety: market must actually be on the correct side of the limit
        if underdog and mkt > threshold + 1e-9:
            return None
        if not underdog and mkt < threshold - 1e-9:
            return None

        return TradeSignal(
            side=side,
            price=fill,
            reason=(
                f"{side.upper()} {regime} {cmp}{threshold:.0%} "
                f"(limit @{fill:.0%}, mkt {mkt:.1%})"
            ),
        )

    def position_risk_fraction(
        self,
        signal: TradeSignal,
        tick: TickContext,
        candle: CandleContext,
    ) -> float:
        lo = float(self.params["min_risk_pct"])
        hot_streak_risk = float(self.params["hot_streak_risk_pct"])
        hi = float(self.params["max_risk_pct"])

        # The engines pass an exact, consecutive settled-win count. The hot
        # streak rule takes priority so both the 3- and 4-win cases are always
        # staked at 25% of initial capital.
        hot_streak = self._wins_streak in (3, 4)

        if hot_streak:
            self._last_size_label = "hot-streak"
            signal.reason = (
                f"{signal.reason} · {self._wins_streak}-win streak "
                f"→ {hot_streak_risk:.0%} of initial"
            )
            return hot_streak_risk

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

        signal.reason = f"{signal.reason} · {detail}"
        return hi if go_big else lo

    def size_label(self, risk_frac: float) -> str:
        return self._last_size_label
