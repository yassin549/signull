"""Thread-safe shared state for the bot, feed, and dashboard."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ActivityEntry:
    timestamp: float
    level: str
    message: str

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "message": self.message,
            "time": time.strftime("%H:%M:%S", time.localtime(self.timestamp)),
        }


@dataclass
class BotSnapshot:
    running: bool = False
    mode: str = "paper"
    asset: str = "btc"
    last_tick_at: float | None = None
    last_error: str | None = None
    ticks: int = 0
    trades_placed: int = 0
    market: dict[str, Any] | None = None
    prices: dict[str, float] | None = None
    signal: dict[str, Any] | None = None
    account: dict[str, Any] | None = None
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    orderbooks: dict[str, Any] = field(default_factory=dict)
    feed: dict[str, Any] = field(default_factory=dict)
    btc: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = field(default_factory=list)
    # Signull 1.0 strategy status (paper + live)
    strategy: dict[str, Any] | None = None
    strategy_trades: list[dict[str, Any]] = field(default_factory=list)


def slice_history_tail(history: list | deque, history_points: int) -> list:
    """Return the last *history_points* items; non-positive length → empty list.

    Python's ``seq[-0:]`` is the full sequence, so callers must not use a bare
    negative slice when ``history_points`` may be 0.
    """
    if history_points is None or history_points <= 0:
        return []
    return list(history)[-history_points:]


class BotState:
    def __init__(self, max_log_entries: int = 200, max_history: int = 7200, max_trades: int = 100):
        self._lock = threading.Lock()
        self._snapshot = BotSnapshot()
        self._log: deque[ActivityEntry] = deque(maxlen=max_log_entries)
        self._price_history: deque[dict] = deque(maxlen=max_history)
        self._btc_history: deque[dict] = deque(maxlen=max_history)
        # Account values are sparse compared with market data. Keeping a
        # separate bounded series provides the dashboard equity curve.
        self._equity_history: deque[dict] = deque(maxlen=max_history)
        self._price_to_beat: float | None = None
        self._bot_stop = threading.Event()
        self._shutdown = threading.Event()
        self._version = 0
        self._feed_updates = 0
        self._feed_window_start = time.time()
        self._feed_window_count = 0
        self._last_history_append = 0.0
        self._last_btc_append = 0.0
        self._last_equity_append = 0.0
        self._history_interval = 0.05  # 20 chart points/sec
        self._btc_history_interval = 0.05  # 20 chart points/sec
        self._last_btc_display: float | None = None
        self._last_chainlink: float | None = None
        self._binance_wall_ts: float = 0.0  # wall clock of last Binance tick
        self._beat_candle_start: int | None = None  # beat locked to this window
        # Closed-candle beat/oracle keyed by candle start_ts (survives clear).
        self._frozen_resolution_refs: dict[int, dict[str, float | None]] = {}

    def request_bot_stop(self) -> None:
        self._bot_stop.set()

    def clear_bot_stop(self) -> None:
        self._bot_stop.clear()

    def should_bot_stop(self) -> bool:
        return self._bot_stop.is_set()

    def request_shutdown(self) -> None:
        self._shutdown.set()
        self._bot_stop.set()

    def should_shutdown(self) -> bool:
        return self._shutdown.is_set()

    # Back-compat aliases used by the bot loop
    def request_stop(self) -> None:
        self.request_bot_stop()

    def clear_stop(self) -> None:
        self.clear_bot_stop()

    def should_stop(self) -> bool:
        return self.should_bot_stop()

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def _bump(self) -> None:
        self._version += 1

    def log(self, level: str, message: str) -> None:
        entry = ActivityEntry(time.time(), level, message)
        with self._lock:
            self._log.appendleft(entry)
            self._bump()

    def update(self, **kwargs: Any) -> BotSnapshot:
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._snapshot, key):
                    setattr(self._snapshot, key, value)
            if "account" in kwargs and kwargs["account"]:
                self._append_equity_from_account_locked(kwargs["account"])
            self._bump()
            return self._snapshot

    def record_equity_point(
        self,
        equity: float,
        *,
        mode: str | None = None,
        force: bool = False,
    ) -> None:
        """Append a balance sample for the dashboard equity curve."""
        with self._lock:
            self._append_equity_locked(
                float(equity),
                mode=str(mode or self._snapshot.mode).lower(),
                force=force,
            )
            self._bump()

    def _append_equity_locked(
        self,
        equity: float,
        *,
        mode: str,
        force: bool = False,
    ) -> None:
        now = time.time()
        previous = self._equity_history[-1] if self._equity_history else None
        # Preserve every balance move; otherwise retain a five-second sample.
        if (
            not force
            and previous
            and previous["v"] == equity
            and now - self._last_equity_append < 5.0
        ):
            return
        self._equity_history.append(
            {"t": now, "v": round(equity, 4), "mode": mode}
        )
        self._last_equity_append = now

    def _append_equity_from_account_locked(self, account: dict[str, Any]) -> None:
        """Record paper equity or the latest observed live USDC balance."""
        mode = str(account.get("mode") or self._snapshot.mode).lower()
        raw = account.get("balance_usdc") if mode == "live" else account.get("paper_equity")
        try:
            equity = float(raw)
        except (TypeError, ValueError):
            return
        self._append_equity_locked(equity, mode=mode, force=False)

    def increment(self, field_name: str, amount: int = 1) -> None:
        with self._lock:
            current = getattr(self._snapshot, field_name, 0)
            setattr(self._snapshot, field_name, current + amount)
            self._bump()

    def get_live_prices(self) -> dict[str, float] | None:
        with self._lock:
            return dict(self._snapshot.prices) if self._snapshot.prices else None

    def get_market(self) -> dict[str, Any] | None:
        """Small feed-owned market read for the latency-sensitive bot loop."""
        with self._lock:
            return dict(self._snapshot.market) if self._snapshot.market else None

    def is_feed_connected(self) -> bool:
        with self._lock:
            return bool(self._snapshot.feed.get("connected"))

    def update_feed_book(self, side: str, bids: list, asks: list) -> None:
        """Update orderbook for 'up' or 'down' outcome from WS book snapshot."""
        now = time.time()
        with self._lock:
            best_bid = max((float(l["price"]) for l in bids), default=0.0)
            best_ask = min((float(l["price"]) for l in asks), default=1.0)
            spread = best_ask - best_bid if bids and asks else 0.0
            mid = (best_bid + best_ask) / 2 if bids and asks else None

            books = dict(self._snapshot.orderbooks)
            books[side] = {
                "bids": bids[:20],
                "asks": asks[:20],
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": round(spread, 4),
                "mid": mid,
                "updated_at": now,
            }
            self._snapshot.orderbooks = books

            if mid is not None:
                prices = dict(self._snapshot.prices or {})
                prices[side] = mid
                self._snapshot.prices = prices
                self._append_history_locked(now, prices, books)

            self._record_feed_update_locked(now)
            self._bump()

    def update_feed_best(self, side: str, best_bid: float, best_ask: float) -> None:
        now = time.time()
        with self._lock:
            books = dict(self._snapshot.orderbooks)
            existing = books.get(side, {})
            spread = best_ask - best_bid
            mid = (best_bid + best_ask) / 2
            books[side] = {
                **existing,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": round(spread, 4),
                "mid": mid,
                "updated_at": now,
            }
            self._snapshot.orderbooks = books

            prices = dict(self._snapshot.prices or {})
            prices[side] = mid
            self._snapshot.prices = prices
            self._append_history_locked(now, prices, books)
            self._record_feed_update_locked(now)
            self._bump()

    def record_trade(self, side: str, price: float, size: float, trade_side: str) -> None:
        now = time.time()
        with self._lock:
            trades = list(self._snapshot.trades)
            trades.insert(0, {
                "t": now,
                "side": side,
                "price": price,
                "size": size,
                "trade_side": trade_side,
            })
            self._snapshot.trades = trades[:100]
            self._record_feed_update_locked(now)
            self._bump()

    def record_strategy_trade(self, trade: dict[str, Any]) -> None:
        """Append a Signull strategy trade (paper or live) for the dashboard."""
        with self._lock:
            trades = list(self._snapshot.strategy_trades)
            trades.insert(0, trade)
            self._snapshot.strategy_trades = trades[:100]
            self._bump()

    def restore_persisted(
        self,
        *,
        strategy_trades: list[dict[str, Any]] | None = None,
        equity_history: list[dict[str, Any]] | None = None,
        trades_placed: int = 0,
    ) -> None:
        """Hydrate dashboard history from a saved session."""
        with self._lock:
            if strategy_trades:
                self._snapshot.strategy_trades = list(strategy_trades)[:100]
            if equity_history:
                self._equity_history.clear()
                for point in equity_history[-self._equity_history.maxlen :]:
                    if isinstance(point, dict) and "v" in point:
                        self._equity_history.append(point)
                if self._equity_history:
                    self._last_equity_append = float(
                        self._equity_history[-1].get("t", 0.0)
                    )
            if trades_placed > 0:
                self._snapshot.trades_placed = int(trades_placed)
            self._bump()

    def set_feed_status(self, connected: bool, extra: dict | None = None) -> None:
        with self._lock:
            feed = dict(self._snapshot.feed)
            feed["connected"] = connected
            if extra:
                feed.update(extra)
            self._snapshot.feed = feed
            self._bump()

    def get_btc_price(self) -> float | None:
        with self._lock:
            btc = self._snapshot.btc
            if not btc:
                return None
            for key in ("chainlink", "price"):
                val = btc.get(key)
                if val is not None:
                    return float(val)
            return None

    def get_resolution_refs(self) -> dict[str, float | None]:
        """Beat / chainlink / spot only — never copies price history."""
        with self._lock:
            return self._resolution_refs_locked()

    def _resolution_refs_locked(self) -> dict[str, float | None]:
        beat = self._price_to_beat
        chainlink = self._last_chainlink
        spot = self._last_btc_display
        btc = self._snapshot.btc or {}
        if chainlink is None and btc.get("chainlink") is not None:
            chainlink = float(btc["chainlink"])
        if spot is None and btc.get("price") is not None:
            spot = float(btc["price"])
        if beat is None and btc.get("price_to_beat") is not None:
            beat = float(btc["price_to_beat"])
        return {
            "beat": float(beat) if beat is not None else None,
            "chainlink": float(chainlink) if chainlink is not None else None,
            "spot": float(spot) if spot is not None else None,
        }

    def freeze_resolution_refs(self, candle_start_ts: int) -> dict[str, float | None]:
        """
        Snapshot beat/oracle for a closing candle *before* clear_market_data.

        Safe to call from feed and bot independently; first freeze wins so a
        later rollover cannot overwrite closed-window refs with the next open.
        """
        key = int(candle_start_ts)
        with self._lock:
            existing = self._frozen_resolution_refs.get(key)
            if existing is not None:
                return dict(existing)
            refs = self._resolution_refs_locked()
            self._frozen_resolution_refs[key] = dict(refs)
            # Bound memory: keep a few hours of 5m windows
            if len(self._frozen_resolution_refs) > 120:
                for old in sorted(self._frozen_resolution_refs.keys())[:-80]:
                    self._frozen_resolution_refs.pop(old, None)
            return dict(refs)

    def get_frozen_resolution_refs(self, candle_start_ts: int) -> dict[str, float | None] | None:
        with self._lock:
            refs = self._frozen_resolution_refs.get(int(candle_start_ts))
            return dict(refs) if refs is not None else None

    def set_price_to_beat(
        self,
        price: float,
        *,
        candle_start_ts: int | None = None,
        force: bool = False,
        estimated: bool = False,
    ) -> bool:
        """
        Lock the candle-open reference price for Δ charting / resolution UI.

        Once set for a given *candle_start_ts*, further calls are ignored so the
        bot cannot overwrite the feed's open beat a few seconds later (which
        pinned Δ ≈ 0 for the rest of the candle).
        """
        with self._lock:
            cs = int(candle_start_ts) if candle_start_ts is not None else None
            if not force:
                if (
                    cs is not None
                    and self._beat_candle_start == cs
                    and self._price_to_beat is not None
                ):
                    return False
                # Without a window id, never clobber an existing beat mid-candle.
                if cs is None and self._price_to_beat is not None:
                    return False

            self._price_to_beat = float(price)
            if cs is not None:
                self._beat_candle_start = cs
            btc = dict(self._snapshot.btc or {})
            if estimated:
                btc["beat_estimated"] = True
            else:
                btc.pop("beat_estimated", None)
            self._snapshot.btc = btc
            self._sync_btc_locked()
            # Seed chart immediately so the series never blanks at candle open.
            self._append_btc_history_locked(time.time(), force=True)
            self._bump()
            return True

    def set_btc_feed_status(self, connected: bool, error: str | None = None) -> None:
        with self._lock:
            self._sync_btc_locked(connected=connected, error=error)
            self._bump()

    def update_btc_price(self, value: float, ts_ms: int) -> None:
        """Fast display price (Polymarket Binance RTDS) — drives the live chart.

        Uses wall-clock timestamps (same clock as outcome price history) so the
        two charts stay time-aligned. Exchange ``ts_ms`` is stored for lag UI.
        """
        wall = time.time()
        with self._lock:
            changed = self._last_btc_display != value
            self._last_btc_display = float(value)
            self._binance_wall_ts = wall
            appended = self._append_btc_history_locked(wall, spot=float(value))
            self._sync_btc_locked(
                price=float(value),
                updated_at=wall,
                source_ts_ms=int(ts_ms),
            )
            # Bump on move *or* history sample so the dashboard keeps streaming
            # even when the spot price is flat for a stretch.
            if changed or appended:
                self._bump()

    def update_btc_chainlink(self, value: float, ts_ms: int) -> None:
        """Resolution oracle — also backfills the chart if Binance goes silent."""
        wall = time.time()
        with self._lock:
            if self._price_to_beat is None:
                self._maybe_set_initial_beat_locked(float(value), wall)
            changed = self._last_chainlink != value
            self._last_chainlink = float(value)
            # Chart from oracle when Binance has never ticked or is stale (>2s).
            binance_stale = (
                self._last_btc_display is None
                or (wall - self._binance_wall_ts) > 2.0
            )
            appended = False
            if binance_stale:
                appended = self._append_btc_history_locked(wall, spot=float(value))
            self._sync_btc_locked(
                chainlink=float(value),
                chainlink_at=wall,
                source_ts_ms=int(ts_ms),
            )
            if changed or appended:
                self._bump()

    def _append_btc_history_locked(
        self,
        now: float,
        *,
        force: bool = False,
        spot: float | None = None,
    ) -> bool:
        """
        Append a chart sample.

        Prefer explicit *spot* (caller-chosen Binance or stale-fallback Chainlink).
        Always stores absolute ``v``; ``d`` when beat is known.
        """
        if not force and now - self._last_btc_append < self._btc_history_interval:
            return False

        if spot is None:
            # Prefer fresh Binance; else last known Chainlink.
            if self._last_btc_display is not None and (now - self._binance_wall_ts) <= 2.0:
                spot = self._last_btc_display
            else:
                spot = self._last_btc_display if self._last_btc_display is not None else self._last_chainlink
        if spot is None:
            return False

        point: dict[str, float] = {"t": now, "v": round(float(spot), 2)}
        beat = self._price_to_beat
        if beat is not None:
            point["d"] = round(float(spot) - float(beat), 2)
        if self._beat_candle_start is not None:
            point["cs"] = int(self._beat_candle_start)

        self._btc_history.append(point)
        self._last_btc_append = now
        return True

    def _maybe_set_initial_beat_locked(self, price: float, now: float) -> None:
        """Only when beat is still unset — lock to market candle window if known."""
        if self._price_to_beat is not None:
            return
        market = self._snapshot.market
        start = market.get("candle_start_ts") if market else None
        if start is not None:
            start = int(start)
            if self._beat_candle_start == start and self._price_to_beat is not None:
                return
            self._beat_candle_start = start
        self._price_to_beat = float(price)
        elapsed = (now - float(start)) if start else 0.0
        if start and elapsed > 30:
            btc = dict(self._snapshot.btc or {})
            btc["beat_estimated"] = True
            self._snapshot.btc = btc
        self._append_btc_history_locked(now, force=True, spot=float(price))

    def _sync_btc_locked(
        self,
        *,
        price: float | None = None,
        updated_at: float | None = None,
        chainlink: float | None = None,
        chainlink_at: float | None = None,
        connected: bool | None = None,
        error: str | None = None,
        source_ts_ms: int | None = None,
    ) -> None:
        btc = dict(self._snapshot.btc or {})
        if price is not None:
            btc["price"] = price
        if updated_at is not None:
            btc["updated_at"] = updated_at
        if chainlink is not None:
            btc["chainlink"] = chainlink
        if chainlink_at is not None:
            btc["chainlink_at"] = chainlink_at
        if connected is not None:
            btc["connected"] = connected
        if error is not None:
            btc["error"] = error
        elif connected:
            btc.pop("error", None)
        if source_ts_ms is not None:
            btc["source_ts_ms"] = source_ts_ms

        beat = self._price_to_beat
        btc["price_to_beat"] = beat
        btc["source"] = "binance+chainlink"
        if beat is not None and not btc.get("beat_estimated"):
            btc.pop("beat_estimated", None)

        # Live Δ uses fast Binance spot vs beat so UI tracks odds movement.
        spot = btc.get("price")
        if spot is None:
            spot = btc.get("chainlink")
        if spot is not None and beat is not None:
            delta = float(spot) - float(beat)
            btc["delta"] = round(delta, 2)
            btc["delta_pct"] = round((delta / beat) * 100, 4) if beat else None
        else:
            btc.pop("delta", None)
            btc.pop("delta_pct", None)

        # Separate oracle Δ (Chainlink) for settlement-aware UI if present.
        oracle = btc.get("chainlink")
        if oracle is not None and beat is not None:
            odelta = float(oracle) - float(beat)
            btc["oracle_delta"] = round(odelta, 2)
        else:
            btc.pop("oracle_delta", None)

        self._snapshot.btc = btc

    def _append_history_locked(self, now: float, prices: dict, books: dict) -> None:
        if now - self._last_history_append < self._history_interval:
            return
        self._last_history_append = now
        up_book = books.get("up", {})
        down_book = books.get("down", {})
        self._price_history.append({
            "t": now,
            "up": prices.get("up"),
            "down": prices.get("down"),
            "up_bid": up_book.get("best_bid"),
            "up_ask": up_book.get("best_ask"),
            "down_bid": down_book.get("best_bid"),
            "down_ask": down_book.get("best_ask"),
        })

    def _record_feed_update_locked(self, now: float) -> None:
        self._feed_updates += 1
        self._feed_window_count += 1
        elapsed = now - self._feed_window_start
        ups = round(self._feed_window_count / elapsed, 1) if elapsed >= 1 else self._feed_window_count
        if elapsed >= 2:
            self._feed_window_start = now
            self._feed_window_count = 0

        feed = dict(self._snapshot.feed)
        feed.update({
            "updates_total": self._feed_updates,
            "updates_per_sec": ups,
            "last_update_at": now,
        })
        self._snapshot.feed = feed

    def clear_market_data(self, *, closing_start_ts: int | None = None) -> None:
        """
        Reset books/history/beat for a new candle.

        If *closing_start_ts* is set, freeze resolution refs for that window
        first so settlement never sees the next open's beat.
        """
        if closing_start_ts is not None:
            self.freeze_resolution_refs(int(closing_start_ts))
        with self._lock:
            self._snapshot.orderbooks = {}
            self._snapshot.trades = []
            self._price_history.clear()
            self._last_btc_append = 0.0
            self._price_to_beat = None
            self._beat_candle_start = None
            # Keep last Binance/Chainlink ticks so the chart can reseed the
            # moment a new beat is set (instead of waiting for the next WS tick).
            self._sync_btc_locked()
            self._bump()

    def _active_btc_candle_start(self) -> int | None:
        market = self._snapshot.market or {}
        raw = market.get("candle_start_ts")
        if raw is not None:
            return int(raw)
        if self._beat_candle_start is not None:
            return int(self._beat_candle_start)
        return None

    def _btc_history_for_dashboard(self, history_points: int) -> list[dict]:
        candle_start = self._active_btc_candle_start()
        points = list(self._btc_history)
        if candle_start is not None:
            points = [
                p
                for p in points
                if p.get("cs") == candle_start
                or (p.get("cs") is None and float(p.get("t", 0)) >= candle_start)
            ]
        return slice_history_tail(points, history_points)

    def get_snapshot(self, history_points: int = 600) -> dict[str, Any]:
        with self._lock:
            data = asdict(self._snapshot)
            data["activity"] = [e.to_dict() for e in list(self._log)]
            data["price_history"] = slice_history_tail(self._price_history, history_points)
            data["btc_history"] = self._btc_history_for_dashboard(history_points)
            data["btc_candle_start_ts"] = self._active_btc_candle_start()
            data["equity_history"] = slice_history_tail(self._equity_history, history_points)
            data["version"] = self._version
            return data
