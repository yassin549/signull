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


class BotState:
    def __init__(self, max_log_entries: int = 200, max_history: int = 7200, max_trades: int = 100):
        self._lock = threading.Lock()
        self._snapshot = BotSnapshot()
        self._log: deque[ActivityEntry] = deque(maxlen=max_log_entries)
        self._price_history: deque[dict] = deque(maxlen=max_history)
        self._btc_history: deque[dict] = deque(maxlen=max_history)
        self._price_to_beat: float | None = None
        self._bot_stop = threading.Event()
        self._shutdown = threading.Event()
        self._version = 0
        self._feed_updates = 0
        self._feed_window_start = time.time()
        self._feed_window_count = 0
        self._last_history_append = 0.0
        self._last_btc_append = 0.0
        self._history_interval = 0.05  # 20 chart points/sec
        self._btc_history_interval = 0.05  # 20 chart points/sec
        self._last_btc_display: float | None = None
        self._last_chainlink: float | None = None

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
            self._bump()
            return self._snapshot

    def increment(self, field_name: str, amount: int = 1) -> None:
        with self._lock:
            current = getattr(self._snapshot, field_name, 0)
            setattr(self._snapshot, field_name, current + amount)
            self._bump()

    def get_live_prices(self) -> dict[str, float] | None:
        with self._lock:
            return dict(self._snapshot.prices) if self._snapshot.prices else None

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

    def set_price_to_beat(self, price: float) -> None:
        with self._lock:
            self._price_to_beat = price
            btc = dict(self._snapshot.btc or {})
            btc.pop("beat_estimated", None)
            self._snapshot.btc = btc
            self._sync_btc_locked()
            self._bump()

    def set_btc_feed_status(self, connected: bool, error: str | None = None) -> None:
        with self._lock:
            self._sync_btc_locked(connected=connected, error=error)
            self._bump()

    def update_btc_price(self, value: float, ts_ms: int) -> None:
        """Fast display price (Polymarket Binance RTDS)."""
        now = ts_ms / 1000.0
        with self._lock:
            changed = self._last_btc_display != value
            self._last_btc_display = value
            self._append_btc_history_locked(now)
            self._sync_btc_locked(price=value, updated_at=now)
            if changed:
                self._bump()

    def update_btc_chainlink(self, value: float, ts_ms: int) -> None:
        """Resolution oracle price — used for beat delta."""
        now = ts_ms / 1000.0
        with self._lock:
            if self._price_to_beat is None:
                self._maybe_set_initial_beat_locked(value, now)
            changed = self._last_chainlink != value
            self._last_chainlink = value
            self._append_btc_history_locked(now)
            self._sync_btc_locked(chainlink=value, chainlink_at=now)
            if changed:
                self._bump()

    def _append_btc_history_locked(self, now: float) -> None:
        if now - self._last_btc_append < self._btc_history_interval:
            return
        beat = self._price_to_beat
        if beat is None:
            return
        ref = self._last_chainlink if self._last_chainlink is not None else self._last_btc_display
        if ref is None:
            return
        point: dict = {"t": now, "d": round(ref - beat, 2)}
        if self._last_btc_display is not None:
            point["v"] = self._last_btc_display
        self._btc_history.append(point)
        self._last_btc_append = now

    def _maybe_set_initial_beat_locked(self, price: float, now: float) -> None:
        market = self._snapshot.market
        if not market:
            return
        start = market.get("candle_start_ts")
        if not start:
            return
        self._price_to_beat = price
        elapsed = now - float(start)
        if elapsed > 30:
            btc = dict(self._snapshot.btc or {})
            btc["beat_estimated"] = True
            self._snapshot.btc = btc

    def _sync_btc_locked(
        self,
        *,
        price: float | None = None,
        updated_at: float | None = None,
        chainlink: float | None = None,
        chainlink_at: float | None = None,
        connected: bool | None = None,
        error: str | None = None,
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

        beat = self._price_to_beat
        btc["price_to_beat"] = beat
        btc["source"] = "binance+chainlink"
        if beat is not None and not btc.get("beat_estimated"):
            btc.pop("beat_estimated", None)
        ref = btc.get("chainlink", btc.get("price"))
        if ref is not None and beat is not None:
            delta = float(ref) - beat
            btc["delta"] = round(delta, 2)
            btc["delta_pct"] = round((delta / beat) * 100, 4) if beat else None
        else:
            btc.pop("delta", None)
            btc.pop("delta_pct", None)
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

    def clear_market_data(self) -> None:
        with self._lock:
            self._snapshot.orderbooks = {}
            self._snapshot.trades = []
            self._price_history.clear()
            self._btc_history.clear()
            self._price_to_beat = None
            self._sync_btc_locked()
            self._bump()

    def get_snapshot(self, history_points: int = 600) -> dict[str, Any]:
        with self._lock:
            data = asdict(self._snapshot)
            data["activity"] = [e.to_dict() for e in list(self._log)]
            data["price_history"] = list(self._price_history)[-history_points:]
            data["btc_history"] = list(self._btc_history)[-history_points:]
            data["version"] = self._version
            return data