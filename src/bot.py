"""Main trading bot loop — Signull strategies on Predict.fun markets."""

from __future__ import annotations

import logging
import queue
import threading
import time
import inspect
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from eth_account import Account

from strategies.base import CandleContext, TickContext
from src.backtest.registry import get_strategy

from .config import BotConfig
from .markets import (
    CandleMarket,
    get_current_candle,
    market_to_dict,
    resolve_candle_winner,
    winner_from_price_refs,
    winner_from_ticks,
)
from .predict_client import PredictClient
from .sizing import (
    cap_stake_for_taker_fee,
    compute_stake,
    estimate_maker_fee,
    estimate_taker_fee,
    scale_pending_for_fill,
)
from .session_store import (
    MAX_EQUITY_HISTORY,
    MAX_STRATEGY_TRADES,
    load_session,
    save_session,
)
from .state import BotState

logger = logging.getLogger(__name__)

BACKTEST_PRICE_FIDELITY_SEC = 60.0


def _settle_pnl(
    stake: float, entry_price: float, won: bool, entry_fee: float = 0.0
) -> float:
    if entry_price <= 0 or stake <= 0:
        return 0.0
    if won:
        return stake / entry_price - stake - entry_fee
    return -stake - entry_fee


@dataclass
class PendingTrade:
    slug: str
    title: str
    side: str
    entry_price: float
    stake: float
    size_label: str
    risk_pct: float
    reason: str
    entry_ts: float
    start_ts: int
    mode: str
    market_id: int
    token_id: str
    order_id: str | None = None
    requested_shares: float = 0.0
    filled_shares: float = 0.0
    entry_fee: float = 0.0
    taker_fee_rate: float = 0.0
    chase_attempts: int = 0
    last_chase_monotonic: float = 0.0
    max_chase_attempts: int = 5
    fee_rate_bps: int = 0


@dataclass
class _SettleResult:
    slug: str
    title: str
    pending: PendingTrade
    winner: str | None
    source: str
    filled_shares: float


class TradingBot:
    def __init__(
        self,
        config: BotConfig,
        state: BotState | None = None,
        *,
        session: dict | None = None,
    ):
        self.config = config
        self.state = state or BotState()
        self.client = PredictClient(config)

        self.strategy = get_strategy(config.strategy_id, config.strategy_params())
        self._initial = float(config.paper_initial_capital)
        self._equity = float(config.paper_initial_capital)
        self._peak = float(config.paper_initial_capital)
        self._wins_recent: list[bool] = []
        self._wins_streak = 0
        self._losses_streak = 0
        self._last_session_save = 0.0
        self._apply_session(session or load_session(config))
        self._bankroll_lock = threading.Lock()
        self._settle_results: queue.Queue[_SettleResult] = queue.Queue()
        self._settling_slugs: set[str] = set()

        self._active_slug: str | None = None
        self._active_start_ts: int | None = None
        self._active_title: str = ""
        self._candle_ticks: list[tuple[int, float, float]] = []
        self._entered = False
        self._pending: PendingTrade | None = None
        self._eval_fidelity_bucket: int | None = None
        self._last_account_refresh = 0.0
        self._last_ai_version = -1
        self._cached_account: dict[str, Any] | None = None
        self._cached_open_orders: list[dict] = []
        self._cached_wallet_balance: float | None = None
        self._live_orders_blocked: str | None = None
        self._order_lock = threading.Lock()
        self._pending_orders_queue: list[PendingTrade] = []
        self._orphan_order_ids: set[str] = set()

        self.state.update(
            running=False,
            mode=config.trading_mode,
            asset=config.asset,
        )
        self._push_strategy_state(signal_side="hold", signal_reason="Starting…")

        if self.config.is_live and self.client.is_authenticated:
            self._reconcile_live_orders()
            self._cleanup_stale_orders()

    def _cleanup_stale_orders(self) -> None:
        """Try to cancel any unfilled orders left from prior runs using SDK batch cancel."""
        try:
            orders = self.client.get_open_orders() or []
        except Exception as exc:
            logger.debug("cleanup fetch failed: %s", exc)
            return
        if not orders:
            return
        active = [o for o in orders if str(o.get("status", "")).upper() in ("PENDING", "LIVE", "OPEN", "")]
        if not active:
            return
        logger.info("Cleanup: %d open order(s) found from prior runs — attempting batch cancel", len(active))
        try:
            ok = self.client.cancel_orders_batch(active, is_neg_risk=False, is_yield_bearing=False)
            if ok:
                logger.info("Batch cancel succeeded — all %d order(s) cancelled", len(active))
                self.state.log("info", f"Cancelled {len(active)} stale order(s) from prior runs")
            else:
                logger.warning("Batch cancel reported failure — orders may still be live")
        except Exception as exc:
            logger.warning("Batch cancel failed: %s", exc)

    def run(self) -> None:
        mode = "LIVE" if self.config.is_live else "PAPER"
        thr = self.strategy.params.get("threshold")
        if thr is not None:
            spec_str = f"limit @{float(thr):.0%}"
        elif self.config.strategy_id == "signull_1_8":
            spec_str = "open favorite"
        elif self.config.strategy_id == "signull_1_9":
            spec_str = "3-min favorite"
        elif self.config.strategy_id == "signull_1_10":
            target = self.strategy.params.get("target_delta", 10.0)
            spec_str = f"regime ±${float(target):.0f}"
        else:
            target = self.strategy.params.get("target_delta", 10.0)
            spec_str = f"target ±${float(target):.0f}"
        msg = (
            f"Bot started [{mode}] {self.strategy.meta.name} — {self.config.asset.upper()}, "
            f"{spec_str}, paper bankroll ${self._initial:.2f}"
        )
        logger.info(msg)
        self.state.log("info", msg)
        self.state.clear_stop()
        self.state.update(running=True)

        while not self.state.should_stop():
            try:
                self._tick()
            except Exception as exc:
                logger.exception("Error in tick")
                self.state.update(last_error=str(exc))
                self.state.log("error", str(exc))
            time.sleep(self.config.bot_poll_interval_sec)

        self.state.update(running=False)
        self.state.log("info", "Bot stopped")
        self.persist_session()

    def _apply_session(self, session: dict | None) -> None:
        if not session:
            return

        if not self.config.is_live:
            self._initial = float(session.get("initial_capital", self._initial))
            self._equity = float(session.get("equity", self._equity))
            self._peak = float(session.get("peak_equity", max(self._peak, self._equity)))

        self._wins_recent = [bool(value) for value in session.get("wins_recent", [])][-10:]
        self._wins_streak = int(session.get("wins_streak", 0))
        self._losses_streak = int(session.get("losses_streak", 0))

        self.state.restore_persisted(
            strategy_trades=session.get("strategy_trades"),
            equity_history=session.get("equity_history"),
            trades_placed=int(session.get("trades_placed", 0)),
        )

        if not self.config.is_live:
            self.state.log("info", f"Restored paper session — equity ${self._equity:.2f} (initial ${self._initial:.2f})")
        else:
            self.state.log("info", "Restored strategy trade history from session")

    def persist_session(self) -> None:
        snap = self.state.get_snapshot(history_points=MAX_EQUITY_HISTORY)
        with self._bankroll_lock:
            payload = {
                "initial_capital": round(self._initial, 4),
                "equity": round(self._equity, 4),
                "peak_equity": round(self._peak, 4),
                "wins_recent": list(self._wins_recent),
                "wins_streak": self._wins_streak,
                "losses_streak": self._losses_streak,
                "trades_placed": int(snap.get("trades_placed", 0)),
                "strategy_trades": list(snap.get("strategy_trades", []))[:MAX_STRATEGY_TRADES],
                "equity_history": list(snap.get("equity_history", []))[-MAX_EQUITY_HISTORY:],
            }
            if self._pending is not None and self._pending.mode == "live":
                p = self._pending
                payload["pending_order"] = {
                    "order_id": p.order_id,
                    "market_id": p.market_id,
                    "token_id": p.token_id,
                    "side": p.side,
                    "entry_price": p.entry_price,
                    "stake": p.stake,
                    "size_label": p.size_label,
                    "risk_pct": p.risk_pct,
                    "reason": p.reason,
                    "entry_ts": p.entry_ts,
                    "start_ts": p.start_ts,
                    "slug": p.slug,
                    "title": p.title,
                    "requested_shares": p.requested_shares,
                    "filled_shares": p.filled_shares,
                    "entry_fee": p.entry_fee,
                    "taker_fee_rate": p.taker_fee_rate,
                    "chase_attempts": p.chase_attempts,
                    "max_chase_attempts": p.max_chase_attempts,
                }
        save_session(self.config, payload)
        self._last_session_save = time.time()

    def _reconcile_live_orders(self) -> None:
        if not self.client.is_authenticated:
            return
        try:
            open_orders = self.client.get_open_orders() or []
        except Exception as exc:
            logger.warning("Failed to fetch open orders for reconciliation: %s", exc)
            return

        session_pending = None
        session = load_session(self.config)
        if session and session.get("pending_order"):
            session_pending = session["pending_order"]

        for order in open_orders:
            order_id = str(order.get("id") or order.get("orderId") or "")
            if not order_id:
                continue
            if order.get("status") not in ("PENDING", "LIVE", "OPEN"):
                continue

            if session_pending and session_pending.get("order_id") == order_id:
                sp = session_pending
                self._pending = PendingTrade(
                    slug=sp["slug"], title=sp["title"], side=sp["side"],
                    entry_price=float(sp["entry_price"]), stake=float(sp["stake"]),
                    size_label=sp["size_label"], risk_pct=float(sp["risk_pct"]),
                    reason=sp["reason"], entry_ts=float(sp["entry_ts"]),
                    start_ts=int(sp["start_ts"]), mode="live",
                    market_id=int(sp.get("market_id", 0)),
                    token_id=sp["token_id"], order_id=order_id,
                    requested_shares=float(sp["requested_shares"]),
                    filled_shares=float(sp.get("filled_shares", 0)),
                    entry_fee=float(sp.get("entry_fee", 0)),
                    taker_fee_rate=float(sp.get("taker_fee_rate", 0)),
                    chase_attempts=int(sp.get("chase_attempts", 0)),
                    max_chase_attempts=int(sp.get("max_chase_attempts", 5)),
                    fee_rate_bps=int(sp.get("fee_rate_bps", 0)),
                )
                self._entered = True
                logger.info("Reconciled live order from session: %s", order_id)
                self.state.log("info", f"Reconciled live order {order_id} @ {sp['entry_price']:.0%}")
                return

    def _reconcile_pending_order(self, market) -> None:
        """Try to find a pending order from wallet monitor data after failed ID parse."""
        if self._pending is not None:
            self._live_orders_blocked = None
            return
        monitored = self.state.get_account() or {}
        orders = monitored.get("open_orders") or []
        if not orders:
            return
        for order in orders:
            raw_id = str(order.get("id") or order.get("orderId") or order.get("orderID") or "")
            if not raw_id:
                continue
            if order.get("status") not in ("PENDING", "LIVE", "OPEN"):
                continue
            raw_side = str(order.get("side") or "").lower()
            side_map = {"buy": "up", "sell": "down"}
            side = side_map.get(raw_side, raw_side)
            raw_price = order.get("price") or order.get("pricePerShare")
            if raw_price is not None:
                try:
                    price = float(raw_price) / 1e18 if float(raw_price) > 1 else float(raw_price)
                except (TypeError, ValueError):
                    continue
            else:
                continue
            raw_size = order.get("originalSize") or order.get("original_size") or order.get("size") or 0
            try:
                size_val = float(raw_size) / 1e18 if float(raw_size) > 1e12 else float(raw_size)
            except (TypeError, ValueError):
                size_val = 0
            stake = size_val * price if price > 0 else 0
            if stake < 0.01:
                continue
            token_id = str(order.get("tokenId") or order.get("token_id") or "")
            market_id = int(order.get("marketId") or order.get("market_id") or 0)
            self._pending = PendingTrade(
                slug=market.slug, title=market.title, side=side,
                entry_price=round(price, 2), stake=round(stake, 4),
                size_label=f"${stake:.2f}", risk_pct=100.0,
                reason="Reconciled from wallet monitor",
                entry_ts=time.time(), start_ts=market.candle_start_ts,
                mode="live", market_id=market_id, token_id=token_id,
                order_id=raw_id,
                requested_shares=size_val,
                filled_shares=0,
                entry_fee=0, taker_fee_rate=self.config.taker_fee_rate,
                last_chase_monotonic=time.monotonic(),
                max_chase_attempts=self.config.max_chase_attempts,
                fee_rate_bps=market.fee_rate_bps,
            )
            self._entered = True
            self._live_orders_blocked = None
            logger.info("Reconciled pending order from wallet monitor: %s", raw_id)
            self.state.log("trade", f"[LIVE] RECONCILED ORDER {side.upper()} @ {price:.0%} ${stake:.2f} from wallet monitor")
            return

    def reset_account(self, initial_capital: float = 100.0) -> None:
        with self._bankroll_lock:
            self._initial = float(initial_capital)
            self._equity = float(initial_capital)
            self._peak = float(initial_capital)
            self._wins_recent = []
            self._wins_streak = 0
            self._losses_streak = 0
            self._pending = None
            self._entered = False
            self._cached_account = None

        self.strategy.on_account_update(
            float(initial_capital), float(initial_capital), float(initial_capital),
            wins_recent=0, wins_streak=0, losses_streak=0,
        )
        self.state.reset_account(initial_capital)
        self.persist_session()
        self.state.log("info", f"Account balance reset to ${initial_capital:.2f} and all prior trading data deleted.")
        self._push_strategy_state(signal_side="hold", signal_reason="Account reset to $100")

    def update_strategy(self, strategy_id: str, params: dict[str, Any] | None = None):
        strat = get_strategy(strategy_id, params)
        self.config.strategy_id = strategy_id
        self.config.custom_strategy_params = dict(strat.params)
        self.strategy = strat
        self._eval_fidelity_bucket = None
        self._last_ai_version = -2
        self._sync_account_to_strategy()
        msg = f"Trading model changed to {strat.meta.name}"
        logger.info("%s with params: %s", msg, strat.params)
        self.state.log("info", f"{msg} (params: {strat.params})")
        self._push_strategy_state(signal_side="hold", signal_reason=f"Model switched to {strat.meta.name}")
        return strat

    def _tick(self) -> None:
        self._drain_settle_results()

        if self.config.is_live and not self.client.is_authenticated:
            self.client.ensure_authenticated()

        market = self._market_from_feed()
        if market is None:
            market = get_current_candle(self.config.asset)
        if market is None:
            self.state.log("warn", f"No active {self.config.asset.upper()} 5M candle")
            return

        if market.slug != self._active_slug:
            self._on_new_candle(market)
        else:
            self._maybe_finalize_closing_candle(market)

        if not self.state.is_feed_connected():
            self._refresh_books_rest(market)

        up_mid, down_mid = self._read_prices(market)
        now_ts = int(time.time())
        self._candle_ticks.append((now_ts, up_mid, down_mid))

        if len(self._candle_ticks) > 2000:
            self._candle_ticks = self._candle_ticks[-1500:]

        ctx = CandleContext(
            slug=market.slug, title=market.title,
            start_ts=market.candle_start_ts, end_ts=int(market.end_date.timestamp()), winner="",
        )
        refs = self.state.get_resolution_refs()
        tick = TickContext(
            t=now_ts, up=up_mid, down=down_mid,
            seconds_into_candle=max(0.0, now_ts - market.candle_start_ts),
            seconds_to_close=max(0.0, market.seconds_to_close),
            btc_price=self.state.get_btc_price(),
            btc_price_to_beat=refs.get("beat"),
            sim_prob=self.state.get_sim_prob(),
        )

        self._sync_account_to_strategy()

        signal = None
        if not self._entered:
            if self.state.has_trade_for_slug(market.slug):
                self._entered = True
            else:
                eval_tick = self._tick_for_strategy_eval(tick)
                if eval_tick is not None:
                    signal = self.strategy.evaluate(eval_tick, ctx, entered=False)

        wait_msg = self._waiting_message()

        signal_side = "hold"
        signal_reason = wait_msg
        if self._pending is not None:
            signal_side = self._pending.side
            signal_reason = (
                f"In position {self._pending.side.upper()} @ "
                f"{self._pending.entry_price:.0%} · "
                f"{self._pending.size_label} ${self._pending.stake:.2f}"
            )
            if self.config.is_live and self._pending.mode == "live" and self._pending.order_id:
                self._chase_pending_order(market, tick)
        elif self._live_orders_blocked == "pending_reconcile":
            signal_reason = "Order placed — reconciling from wallet…"
            self._reconcile_pending_order(market)
        elif signal is not None:
            signal_side = signal.side
            signal_reason = signal.reason

        if time.time() - self._last_account_refresh >= 1:
            self._cached_account = self._build_account_snapshot()
            self._last_account_refresh = time.time()
        account_data = self._cached_account or self._build_account_snapshot()
        monitored = self.state.get_account() or {}
        raw_bal = monitored.get("balance_usdt")
        if raw_bal is not None:
            try:
                self._cached_wallet_balance = float(raw_bal)
            except (TypeError, ValueError):
                pass

        self.state.update(
            last_tick_at=time.time(),
            last_error=None,
            market=market_to_dict(market),
            prices={"up": up_mid, "down": down_mid},
            signal={
                "side": signal_side,
                "price": signal.price if signal else (self._pending.entry_price if self._pending else 0.0),
                "reason": signal_reason,
            },
            open_orders=list(monitored.get("open_orders") or [])[:20],
        )
        self.state.merge_account(account_data)
        self.state.increment("ticks")
        self._push_strategy_state(signal_side=signal_side, signal_reason=signal_reason)

        if signal is not None and not self._entered:
            with self._order_lock:
                if not self._entered:
                    self._enter(market, signal, tick, ctx)
                else:
                    self._pending_orders_queue.append(signal)

        if time.time() - self._last_session_save >= 30.0:
            self.persist_session()
            # Also try to cancel orphaned orders every 30s
            if self._orphan_order_ids:
                self._cleanup_stale_orders()

    def _waiting_message(self) -> str:
        if self.config.strategy_id == "signull_1_11":
            return "AI model is reading the BTC chart…"
        if self.config.strategy_id == "signull_1_5":
            target = float(self.strategy.params.get("target_delta", 10.0))
            return f"Waiting for BTC to move ±${target:.0f} on the chart…"
        if self.config.strategy_id == "signull_1_8":
            return "Waiting for the opening favorite…"
        if self.config.strategy_id == "signull_1_9":
            return "Waiting for the 3-minute favorite…"
        if self.config.strategy_id == "signull_1_10":
            target = float(self.strategy.params.get("target_delta", 10.0))
            return f"Waiting for a ±${target:.0f} break (regime-adaptive)…"
        if self.config.strategy_id == "signull_1_7":
            return "Waiting for BTC to cross this candle's adaptive point-of-no-return..."
        if self.config.strategy_id == "signull_1_6":
            thr = float(self.strategy.params.get("threshold", 0.65))
            return f"Waiting for Option SIM 1.1 probability to reach ≥ {thr:.0%}…"
        thr = float(self.strategy.params.get("threshold", 0.70))
        if self.config.strategy_id == "signull_1_1":
            late = float(self.strategy.params.get("late_entry_seconds", 2.0))
            return f"Watching for ≥ {thr:.0%} or end-of-candle entry (last {late:.0f}s)…"
        if thr < 0.5:
            return f"Waiting for a side to drop ≤ {thr:.0%}…"
        return f"Waiting for a side to reach ≥ {thr:.0%}…"

    def _live_sample_sec(self) -> float:
        raw = self.strategy.params.get("live_sample_sec")
        if raw is None:
            if self.config.strategy_id == "signull_1_1":
                return BACKTEST_PRICE_FIDELITY_SEC
            return 0.0
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0

    def _tick_for_strategy_eval(self, tick: TickContext) -> TickContext | None:
        sample_sec = self._live_sample_sec()
        if sample_sec <= 0:
            return tick

        bucket = int(tick.seconds_into_candle // sample_sec)
        if self._eval_fidelity_bucket is None:
            if tick.seconds_into_candle < sample_sec:
                return None
            self._eval_fidelity_bucket = bucket
            return tick
        if bucket <= self._eval_fidelity_bucket:
            return None
        self._eval_fidelity_bucket = bucket
        return tick

    def _snapshot_resolution_refs(self, start_ts: int | None) -> dict[str, float | None]:
        if start_ts is not None:
            frozen = self.state.get_frozen_resolution_refs(int(start_ts))
            if frozen is not None:
                return frozen
            return self.state.freeze_resolution_refs(int(start_ts))
        return self.state.get_resolution_refs()

    def _market_from_feed(self) -> CandleMarket | None:
        raw = self.state.get_market()
        if not raw or raw.get("provisional") or not raw.get("up_token_id"):
            return None
        try:
            market = CandleMarket(
                slug=str(raw["slug"]), title=str(raw["title"]),
                end_date=datetime.fromisoformat(str(raw["end_date"]).replace("Z", "+00:00")),
                condition_id=str(raw["condition_id"]), up_token_id=str(raw["up_token_id"]),
                down_token_id=str(raw["down_token_id"]), up_price=float(raw.get("up_price", .5)),
                down_price=float(raw.get("down_price", .5)), tick_size=str(raw.get("tick_size", ".01")),
                accepting_orders=bool(raw.get("accepting_orders", False)),
                market_id=int(raw.get("market_id", 0)),
                fee_rate_bps=int(raw.get("fee_rate_bps", 200)),
            )
            return market if market.seconds_to_close > 0 else None
        except (KeyError, TypeError, ValueError):
            return None

    def _on_new_candle(self, market: CandleMarket) -> None:
        prev_slug = self._active_slug
        prev_title = self._active_title
        prev_start = self._active_start_ts
        prev_ticks = list(self._candle_ticks)
        prev_pending = self._pending

        start_ts = prev_start
        if prev_slug is not None:
            try:
                start_ts = int(str(prev_slug).rsplit("-", 1)[-1])
            except ValueError:
                start_ts = prev_start or market.candle_start_ts - 300

        prev_refs = self._snapshot_resolution_refs(start_ts if prev_slug else None)

        self._active_slug = market.slug
        self._active_start_ts = market.candle_start_ts
        self._active_title = market.title
        self._candle_ticks = []
        self._eval_fidelity_bucket = None

        if prev_slug is not None:
            # If we had a live pending order from the previous candle, check
            # whether it is still on the exchange — if so, remember it so we
            # never place a competing entry.
            if prev_pending is not None and prev_pending.mode == "live" and prev_pending.order_id:
                try:
                    still_open = self.client.get_open_orders() or []
                    for o in still_open:
                        oid = str(o.get("id") or o.get("orderId") or o.get("orderHash") or "")
                        if oid and (oid == prev_pending.order_id or oid.replace("0x", "") == prev_pending.order_id.replace("0x", "")):
                            self._orphan_order_ids.add(prev_pending.order_id)
                            logger.warning(
                                "Orphaned order %s from candle %s still live — tracking as orphan",
                                prev_pending.order_id, prev_slug,
                            )
                            break
                except Exception:
                    pass

            self._entered = False
            self._pending = None
            self._schedule_candle_settlement(
                slug=prev_slug, title=prev_title or prev_slug,
                start_ts=int(start_ts), ticks=prev_ticks,
                pending=prev_pending, refs=prev_refs,
            )

        ensure_candle = getattr(self.strategy, "ensure_current_candle", None)
        if callable(ensure_candle):
            ensure_candle(market.slug)
        refresh_klines = getattr(self.strategy, "schedule_klines_refresh", None)
        if callable(refresh_klines):
            refresh_klines(int(time.time()))

        feed_m = self.state.get_market() or {}
        feed_already = (
            feed_m.get("slug") == market.slug
            and not feed_m.get("provisional")
            and feed_m.get("up_token_id")
        )
        if not feed_already:
            beat = self.state.get_btc_price()
            self.state.clear_market_data(closing_start_ts=start_ts if prev_slug else None)
            if beat is not None:
                self.state.set_price_to_beat(beat, candle_start_ts=market.candle_start_ts)
        else:
            beat = self.state.get_btc_price()
            if beat is not None:
                self.state.set_price_to_beat(beat, candle_start_ts=market.candle_start_ts)

        self.state.update(
            market=market_to_dict(market), prices=None,
            signal={"side": "hold", "reason": f"New candle — {self.strategy.meta.name} watching"},
        )
        msg = f"New candle: {market.title}"
        logger.info("%s (closes in %.0fs)", msg, market.seconds_to_close)
        self.state.log("info", msg)
        self._push_strategy_state(signal_side="hold", signal_reason="New candle — watching")

    def _maybe_finalize_closing_candle(self, market: CandleMarket) -> None:
        if self._pending is None or self._active_slug is None:
            return
        if self._pending.slug != self._active_slug:
            return
        close_window = max(2.0, float(self.config.bot_poll_interval_sec) + 0.5)
        if market.seconds_to_close > close_window:
            return
        start_ts = self._active_start_ts or market.candle_start_ts
        self._schedule_candle_settlement(
            slug=self._active_slug, title=self._active_title or market.title,
            start_ts=int(start_ts), ticks=list(self._candle_ticks),
            pending=self._pending, refs=self._snapshot_resolution_refs(int(start_ts)),
        )

    def _schedule_candle_settlement(
        self, *, slug: str, title: str, start_ts: int,
        ticks: list[tuple[int, float, float]], pending: PendingTrade | None,
        refs: dict[str, float | None] | None,
    ) -> None:
        if pending is None or pending.slug != slug:
            return
        with self._bankroll_lock:
            if slug in self._settling_slugs:
                return
            self._settling_slugs.add(slug)
        threading.Thread(
            target=self._finalize_candle,
            kwargs={"slug": slug, "title": title, "start_ts": start_ts,
                    "ticks": ticks, "pending": pending, "refs": refs},
            daemon=True, name="signull-settle",
        ).start()

    def _finalize_candle(
        self, *, slug: str, title: str, start_ts: int,
        ticks: list[tuple[int, float, float]], pending: PendingTrade | None = None,
        refs: dict[str, float | None] | None = None,
    ) -> None:
        try:
            noisy = self.strategy.register_closed_candle(slug, ticks)
            self.state.log("info", f"Candle closed {slug[-12:]} · trust path {'NOISY' if noisy else 'clean'}")

            if pending is None or pending.slug != slug:
                return

            filled = 0.0
            if pending.mode == "live":
                filled = self._close_live_order(pending)
                if filled <= 0:
                    self.state.log("info", f"[LIVE] no fill for {pending.order_id or slug}; no trade settled")
                    return

            frozen = self.state.get_frozen_resolution_refs(start_ts)
            use_refs = frozen if frozen is not None else (refs or {})

            winner, source = self._resolve_winner_reliable(start_ts=start_ts, ticks=ticks, refs=use_refs)
            self._apply_settle_result(
                _SettleResult(slug=slug, title=title, pending=pending, winner=winner, source=source, filled_shares=filled)
            )
        finally:
            with self._bankroll_lock:
                self._settling_slugs.discard(slug)

    def _drain_settle_results(self) -> None:
        while True:
            try:
                result = self._settle_results.get_nowait()
            except queue.Empty:
                break
            self._apply_settle_result(result)

    def _apply_settle_result(self, result: _SettleResult) -> None:
        pending = result.pending
        if result.winner is None:
            self.state.log("warn", f"Could not resolve winner for {result.slug} — voiding paper stake (no PnL change)")
            return

        stake = pending.stake
        if pending.mode == "live" and result.filled_shares > 0:
            stake, _shares = scale_pending_for_fill(
                pending.stake, pending.requested_shares, result.filled_shares, pending.entry_price,
            )
            pending.stake = stake
            pending.filled_shares = result.filled_shares
            pending.entry_fee = estimate_taker_fee(stake, pending.entry_price, pending.taker_fee_rate)

        won = pending.side == result.winner
        pnl = _settle_pnl(stake, pending.entry_price, won, pending.entry_fee)
        self._notify_trade_settled(won, {
            "slug": result.slug,
            "winner": result.winner,
            "side": pending.side,
            "entry_price": pending.entry_price,
            "stake": round(stake, 4),
            "pnl": round(pnl, 4),
        })

        with self._bankroll_lock:
            self._equity += pnl
            self._peak = max(self._peak, self._equity)
            self._losses_streak = 0 if won else self._losses_streak + 1
            self._wins_recent.append(won)
            if len(self._wins_recent) > 10:
                self._wins_recent.pop(0)
            self._wins_streak = self._wins_streak + 1 if won else 0
            equity_after = self._equity

        trade_rec = {
            "t": time.time(), "slug": result.slug, "title": result.title,
            "side": pending.side, "entry_price": pending.entry_price,
            "stake": round(stake, 4), "entry_fee": round(pending.entry_fee, 4),
            "size_label": pending.size_label, "risk_pct": pending.risk_pct,
            "winner": result.winner, "won": won,
            "pnl": round(pnl, 4),
            "equity_after": round(equity_after, 4),
            "mode": pending.mode, "reason": pending.reason,
            "resolve_source": result.source,
            "filled_shares": result.filled_shares if pending.mode == "live" else None,
        }
        self.state.record_strategy_trade(trade_rec)
        self.state.increment("trades_placed")

        if pending.mode == "paper":
            self.state.record_equity_point(equity_after, mode=self.config.trading_mode, force=True)
        if self._pending is not None and self._pending.slug == result.slug:
            with self._order_lock:
                self._pending = None
                self._entered = False
                if self._pending_orders_queue:
                    self._pending_orders_queue.pop(0)

        result_label = "WIN" if won else "LOSS"
        msg = (
            f"[{pending.mode.upper()}] {result_label} {pending.side.upper()} "
            f"(winner={result.winner} via {result.source}) "
            f"@ {pending.entry_price:.0%} stake ${stake:.2f} "
            f"pnl {pnl:+.2f} → equity ${equity_after:.2f}"
        )
        if pending.mode == "live":
            msg = (
                f"[LIVE] {result_label} {pending.side.upper()} "
                f"(winner={result.winner} via {result.source}) "
                f"confirmed fill {result.filled_shares:.2f} shares "
                f"(${stake:.2f}); wallet settlement pending"
            )
        self.state.log("trade" if won else "warn", msg)
        logger.info(msg)
        self.persist_session()
        self._cached_account = self._build_account_snapshot()
        self.state.update(account=self._cached_account, signal={"side": "hold", "reason": msg})
        self._push_strategy_state(signal_side="hold", signal_reason=msg)

    def _position_shares_for(self, pending: PendingTrade) -> float:
        """Filled shares for this candle, read from the account's positions.

        The exchange's per-order endpoint can 404 after an order leaves the book,
        so positions are the reliable source of what actually filled.
        """
        try:
            positions = self.client.get_positions()
        except Exception:
            return 0.0
        total = 0.0
        for p in positions or []:
            market = p.get("market") or {}
            matched_market = (
                (pending.market_id and int(market.get("id", 0) or 0) == int(pending.market_id))
                or (pending.slug and str(market.get("categorySlug") or "") == pending.slug)
            )
            if not matched_market:
                continue
            outcome = p.get("outcome") or {}
            name = str(outcome.get("name") or "").lower()
            if name and pending.side and name != str(pending.side).lower():
                continue
            try:
                amt = float(p.get("amount", 0) or 0)
            except (TypeError, ValueError):
                continue
            if amt >= 1e12:
                amt = amt / 1e18
            total += amt
        return total

    def _close_live_order(self, pending: PendingTrade) -> float:
        if not pending.order_id:
            return 0.0
        try:
            order = self.client.get_order(pending.order_id)
            matched = float(order.get("sizeMatched", 0) or order.get("takerAmount", 0) or 0)
            if order.get("_fallback"):
                matched = 0.0
                orders_list = self._cached_open_orders or self.client.get_open_orders() or []
                for o in orders_list:
                    oid = str(o.get("id") or o.get("orderId") or o.get("orderHash") or "")
                    if oid and (oid == pending.order_id or oid.replace("0x", "") == pending.order_id.replace("0x", "")):
                        for key in ("sizeMatched", "matchedSize", "filledSize", "takerAmount"):
                            if o.get(key) is not None:
                                matched = float(o[key])
                                if matched >= 1e12:
                                    matched = matched / 1e18
                                break
                        break
            if matched <= 0:
                pos_shares = self._position_shares_for(pending)
                if pos_shares > 0:
                    matched = pos_shares
            if matched > 0:
                pending.filled_shares = matched
                return matched
            status = str(order.get("status", "")).lower()
            if status in ("matched", "filled", "cancelled", "canceled", "expired"):
                return pending.filled_shares
            if order.get("_fallback"):
                self.client.cancel_order(pending.order_id)
                return pending.filled_shares
            time.sleep(2.0)
            order = self.client.get_order(pending.order_id)
            matched = float(order.get("sizeMatched", 0) or order.get("takerAmount", 0) or 0)
            if matched > 0:
                pending.filled_shares = matched
            status = str(order.get("status", "")).lower()
            if status not in ("matched", "filled", "cancelled", "canceled", "expired"):
                self.client.cancel_order(pending.order_id, order_data=order)
                order = self.client.get_order(pending.order_id)
                matched = float(order.get("sizeMatched", 0) or order.get("takerAmount", 0) or 0)
                if matched > 0:
                    pending.filled_shares = matched
        except Exception:
            logger.exception("Unable to reconcile/cancel live order %s", pending.order_id)
        return pending.filled_shares

    def _resolve_winner_reliable(
        self, *, start_ts: int, ticks: list[tuple[int, float, float]], refs: dict[str, float | None],
    ) -> tuple[str | None, str]:
        winner, source = winner_from_price_refs(refs)
        if winner is not None:
            return winner, source

        for attempt in range(8):
            winner = resolve_candle_winner(self.config.asset, start_ts, require_resolved=True)
            if winner is not None:
                return winner, "predict_fun"
            if attempt < 7:
                time.sleep(0.35)

        tick_winner = winner_from_ticks(ticks, at_close=True)
        if tick_winner is not None:
            return tick_winner, "ticks"

        return None, "none"

    def _enter(
        self, market: CandleMarket, signal, tick: TickContext, ctx: CandleContext,
    ) -> None:
        # Hard block if we know about orphaned orders from prior candles
        if self._orphan_order_ids:
            self.state.log("warn",
                f"{len(self._orphan_order_ids)} orphaned order(s) still on predict.fun — "
                "entry blocked. Cancel them on the web UI or restart the bot."
            )
            self._entered = True
            return

        # Safety net: prevent double-trade on same candle slug
        if self.state.has_trade_for_slug(market.slug):
            self._entered = True
            return

        with self._bankroll_lock:
            equity = self._equity
            initial = self._initial
        token_id = market.up_token_id if signal.side == "up" else market.down_token_id
        mode = "live" if self.config.is_live else "paper"
        entry_price = max(0.01, min(0.99, float(signal.price)))

        if self.config.is_live:
            best_ask = None
            if market.market_id > 0:
                try:
                    best_ask = self.client.get_best_ask_for_side(market.market_id, signal.side)
                except Exception:
                    pass
            if best_ask is not None and 0 < best_ask < 1:
                entry_price = max(entry_price, float(best_ask))
            entry_price = round(max(0.01, min(0.99, entry_price)), 2)
            signal.price = entry_price

            if tick.seconds_to_close < 10.0:
                self.state.log("warn", f"Too close to candle close ({tick.seconds_to_close:.0f}s) — skipping entry")
                self._entered = True
                return

        wallet_balance = self._cached_wallet_balance
        if self.config.is_live and wallet_balance is None and self.client.is_authenticated:
            try:
                wallet_balance = self.client.get_balance_usdt()
                if wallet_balance is not None:
                    self._cached_wallet_balance = wallet_balance
            except Exception:
                logger.debug("wallet balance read failed", exc_info=True)

        override_fn = getattr(self.strategy, "stake_override", None)
        override = None
        if callable(override_fn):
            try:
                override = override_fn(
                    signal, tick, ctx,
                    equity=equity, initial=initial, wallet_balance=wallet_balance,
                )
            except TypeError:
                override = None
            except Exception:
                logger.exception("stake_override failed")
                override = None

        if override is not None:
            stake, size_label, risk_frac = override
            risk_frac = float(risk_frac)
        elif self.config.use_fixed_stake:
            fixed_val = self.config.fixed_stake_usdc
            risk_frac = 0.0
            size_label = f"${fixed_val:.2f} fixed"
            stake = compute_stake(
                0.0, initial, equity, wallet_balance=wallet_balance,
                is_live=self.config.is_live, fixed_stake=fixed_val,
            )
        else:
            risk_frac = self.strategy.position_risk_fraction(signal, tick, ctx)
            size_label = self.strategy.size_label(risk_frac)
            stake = compute_stake(
                risk_frac, initial, equity,
                wallet_balance=wallet_balance, is_live=self.config.is_live,
            )

        available_cash = equity
        if self.config.is_live and wallet_balance is not None:
            available_cash = min(available_cash, wallet_balance)
        fee_rate = self.config.taker_fee_rate
        stake = cap_stake_for_taker_fee(stake, entry_price, fee_rate, available_cash)

        if self.config.is_live and entry_price > 0:
            if available_cash < stake:
                self.state.log("warn", f"Insufficient USDT balance (${available_cash:.2f}) for order stake (${stake:.2f}) — skipping")
                self._entered = True
                return

        if stake < 0.01:
            self.state.log("warn", "Stake too small — skipping")
            self._entered = True
            return

        fee_rate = self.config.maker_fee_rate if entry_price == float(signal.price) else self.config.taker_fee_rate
        entry_fee = estimate_maker_fee(stake, entry_price, fee_rate) if fee_rate < 0 else estimate_taker_fee(stake, entry_price, fee_rate)
        order_id: str | None = None

        if self.config.is_live:
            if self._live_orders_blocked:
                if self._live_orders_blocked == "pending_reconcile":
                    return  # silently wait for wallet monitor reconciliation
                self.state.log("error", self._live_orders_blocked)
                self._entered = True
                return
            if not self.client.is_authenticated:
                if not self.client.ensure_authenticated():
                    self.state.log("error", "Live mode but wallet not authenticated")
                    self._entered = True
                    return

            # Fetch LIVE open orders from API (not cached state) and hard-block if any exist
            live_open_orders = []
            try:
                live_open_orders = self.client.get_open_orders() or []
            except Exception:
                pass
            active_order_ids = []
            total_live_exposure = 0.0
            for o in live_open_orders:
                status = str(o.get("status", "")).upper()
                if status in ("PENDING", "LIVE", "OPEN", ""):
                    oid = str(o.get("id") or o.get("orderId") or o.get("orderHash") or "")
                    if oid:
                        active_order_ids.append(oid)
                    raw_stake = o.get("makerAmount", 0) or o.get("takerAmount", 0) or o.get("originalSize", 0) or 0
                    if raw_stake:
                        try:
                            raw_stake = int(raw_stake)
                            if raw_stake >= 1e12:
                                total_live_exposure += raw_stake / 1e18
                            elif raw_stake > 1:
                                total_live_exposure += raw_stake
                        except (TypeError, ValueError):
                            pass
                    elif o.get("originalQuantity"):
                        try:
                            total_live_exposure += float(o["originalQuantity"])
                        except (TypeError, ValueError):
                            pass

            if active_order_ids:
                # Stale/zero-exposure orders (e.g. cancelled or expired but still
                # listed) must not block a new candle. Try to cancel whatever is
                # there, and only refuse to trade if a real position cannot be cleared.
                try:
                    cleared = self.client.cancel_orders_batch(
                        live_open_orders, is_neg_risk=False, is_yield_bearing=False
                    )
                except Exception as exc:
                    logger.debug("Stale-order cancel failed: %s", exc)
                    cleared = False

                if cleared:
                    logger.info(
                        "Cleared %d stale open order(s) (was ~$%.2f) — proceeding with entry",
                        len(active_order_ids), total_live_exposure,
                    )
                elif total_live_exposure >= 0.01:
                    self.state.log("warn",
                        f"{len(active_order_ids)} unfilled order(s) (${total_live_exposure:.2f} total) from prior candles remain on predict.fun — "
                        "could not cancel, skipping entry. Use predict.fun web UI to cancel manually."
                    )
                    logger.warning(
                        "BLOCKED entry: %d unfilled orders remain (IDs: %s, total ~$%.2f)",
                        len(active_order_ids), active_order_ids[:3], total_live_exposure,
                    )
                    self._entered = True
                    return
                else:
                    logger.warning(
                        "Ignoring %d zero-exposure stale order(s) (IDs: %s) — proceeding with entry",
                        len(active_order_ids), active_order_ids[:3],
                    )

            try:
                resp = self.client.place_limit_buy(
                    market_id=market.market_id,
                    token_id=token_id,
                    price=entry_price,
                    size_usdt=stake,
                    fee_rate_bps=market.fee_rate_bps,
                    is_neg_risk=False,
                    is_yield_bearing=False,
                )
                order_id = str(resp.get("orderID", ""))
                if not order_id and "error" not in resp:
                    raw = resp.get("raw", {}) or {}
                    order_id = (
                        str(raw.get("data", {}).get("id", ""))
                        or str(raw.get("id", ""))
                        or str(raw.get("orderId", ""))
                        or str(raw.get("orderID", ""))
                        or str(raw.get("data", {}).get("order", {}).get("id", ""))
                    )
                if not order_id and "error" not in resp:
                    # API returned 201 but ID not in expected location — try to
                    # reconcile from wallet monitor's open orders on next tick
                    logger.info("Order placed but ID not parsed: %s", resp)
                    self._live_orders_blocked = "pending_reconcile"
                    self._entered = True
                    # Leave _pending as None; _reconcile_pending_order will
                    # find it from wallet monitor data
                    return
                if not order_id:
                    error_detail = resp.get("error", "no order id returned")
                    raise RuntimeError(f"Order placement failed: {error_detail}")
                logger.info("Live order: %s", resp)
                self.state.log("trade", f"[LIVE] LIMIT BUY {signal.side.upper()} @ {entry_price:.0%} ${stake:.2f} ({size_label})")
            except Exception as exc:
                detail = str(exc)
                self.state.log("error", f"Order failed: {detail}")
                logger.exception("place_limit_buy failed")
                self._entered = True
                return
        else:
            self.state.log("paper", f"[PAPER] FILL {signal.side.upper()} @ {entry_price:.0%} ${stake:.2f} ({size_label}) · {signal.reason}")
            logger.info("[PAPER] %s @ %.2f stake=%.2f %s", signal.side, entry_price, stake, size_label)

        self._pending = PendingTrade(
            slug=market.slug, title=market.title, side=signal.side,
            entry_price=entry_price, stake=stake, size_label=size_label,
            risk_pct=round(risk_frac * 100, 2), reason=signal.reason,
            entry_ts=time.time(), start_ts=market.candle_start_ts,
            mode=mode, market_id=market.market_id, token_id=token_id,
            order_id=order_id,
            requested_shares=stake / entry_price if entry_price > 0 else 0.0,
            entry_fee=entry_fee, taker_fee_rate=fee_rate,
            last_chase_monotonic=time.monotonic(),
            max_chase_attempts=self.config.max_chase_attempts,
            fee_rate_bps=market.fee_rate_bps,
        )
        self._entered = True
        self._push_strategy_state(signal_side=signal.side, signal_reason=signal.reason)

    def _sync_account_to_strategy(self) -> None:
        with self._bankroll_lock:
            equity = self._equity
            initial = self._initial
            peak = self._peak
            wins = sum(self._wins_recent)
            wins_streak = self._wins_streak
            losses = self._losses_streak
        self.strategy.on_account_update(
            equity, initial, peak,
            wins_recent=wins, wins_streak=wins_streak, losses_streak=losses,
            equity_momentum=0.0,
        )

    def _push_strategy_state(self, *, signal_side: str, signal_reason: str) -> None:
        pending = None
        if self._pending is not None:
            p = self._pending
            pending = {
                "side": p.side, "entry_price": p.entry_price, "stake": p.stake,
                "size_label": p.size_label, "risk_pct": p.risk_pct, "mode": p.mode,
                "slug": p.slug, "entry_fee": p.entry_fee, "entry_ts": p.entry_ts,
                "requested_shares": p.requested_shares, "filled_shares": p.filled_shares,
                "taker_fee_rate": p.taker_fee_rate,
            }
        with self._bankroll_lock:
            equity = self._equity
            initial = self._initial
            peak = self._peak
            losses = self._losses_streak
            wins = sum(self._wins_recent)
            wins_streak = self._wins_streak
        self.state.update(strategy={
            "id": self.strategy.meta.id, "name": self.strategy.meta.name,
            "mode": self.config.trading_mode, "params": dict(self.strategy.params),
            "equity": round(equity, 4), "initial": initial, "peak": round(peak, 4),
            "return_pct": round((equity / initial - 1.0) * 100, 2) if initial else 0.0,
            "pending": pending, "entered_this_candle": self._entered,
            "signal_side": signal_side, "signal_reason": signal_reason,
            "losses_streak": losses, "wins_streak": wins_streak, "wins_recent": wins,
        })
        self._push_ai_state()

    def _push_ai_state(self) -> None:
        """Publish the AI model monitor state only when the strategy changes it."""
        snapshot_fn = getattr(self.strategy, "ai_snapshot", None)
        if not callable(snapshot_fn):
            if self._last_ai_version != -1:
                self._last_ai_version = -1
                self.state.update(ai=None)
            return
        version = int(getattr(self.strategy, "_ai_version", 0))
        if version == self._last_ai_version:
            return
        self._last_ai_version = version
        try:
            payload = snapshot_fn()
        except Exception:
            logger.exception("ai_snapshot failed")
            return
        self.state.update(ai=payload)

    def _notify_trade_settled(self, won: bool, info: dict) -> None:
        """Call on_trade_settled, passing settle info only if the strategy accepts it."""
        handler = getattr(self.strategy, "on_trade_settled", None)
        if not callable(handler):
            return
        try:
            accepts_info = "info" in inspect.signature(handler).parameters
        except (TypeError, ValueError):
            accepts_info = False
        try:
            if accepts_info:
                handler(won, info=info)
            else:
                handler(won)
        except Exception:
            logger.exception("on_trade_settled failed")

    def _read_prices(self, market: CandleMarket) -> tuple[float, float]:
        live = self.state.get_live_prices()
        if live and "up" in live and "down" in live:
            return float(live["up"]), float(live["down"])
        if market.market_id > 0:
            try:
                up_mid = self.client.get_midpoint(market.market_id)
                return up_mid, round(1.0 - up_mid, 4)
            except Exception:
                pass
        return float(market.up_price), float(market.down_price)

    def _refresh_books_rest(self, market: CandleMarket) -> None:
        if market.market_id <= 0:
            return
        try:
            book = self.client.get_order_book(market.market_id)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            from .feed import _normalize_levels, _complement_levels
            up_bids = _normalize_levels(bids)
            up_asks = _normalize_levels(asks)
            if up_bids or up_asks:
                self.state.update_feed_book("up", up_bids, up_asks)
                down_bids = _complement_levels(asks, market.tick_size)
                down_asks = _complement_levels(bids, market.tick_size)
                self.state.update_feed_book("down", down_bids, down_asks)
        except Exception:
            logger.debug("REST book refresh failed", exc_info=True)

    def _chase_pending_order(self, market: CandleMarket, tick: TickContext) -> None:
        pending = self._pending
        if not pending or not pending.order_id or pending.mode != "live":
            return
        monotonic_now = time.monotonic()
        if monotonic_now - pending.last_chase_monotonic < self.config.chase_interval_sec:
            return
        if tick.seconds_to_close < 15.0:
            return
        if pending.chase_attempts >= pending.max_chase_attempts:
            if pending.chase_attempts == pending.max_chase_attempts:
                logger.warning("Max chase attempts (%d) reached for order %s", pending.max_chase_attempts, pending.order_id)
                self.state.log("warn", f"Max chase attempts reached for {pending.order_id}")
                pending.chase_attempts += 1
            return

        monitored = self.state.get_account() or {}
        open_orders = monitored.get("open_orders") or []
        if not open_orders:
            try:
                open_orders = self.client.get_open_orders() or []
            except Exception:
                pass

        order_key = str(pending.order_id).lower()
        order_found = False
        for o in open_orders:
            oid = str(o.get("id") or o.get("orderId") or o.get("orderHash") or "").lower()
            if order_key in oid or oid in order_key:
                order_found = True
                break

        if not order_found:
            logger.info("Order %s not in open orders — assuming filled/expired, stopping chase", pending.order_id)
            self.state.log("info", f"Order no longer open — stopping chase")
            pending.chase_attempts = pending.max_chase_attempts
            return

        try:
            best_ask = self.client.get_best_ask_for_side(market.market_id, pending.side) if market.market_id > 0 else None
        except Exception:
            return
        if best_ask is None or not (0 < best_ask < 1):
            return
        new_price = min(0.99, float(best_ask))
        if abs(new_price - pending.entry_price) < 0.005:
            return

        cancel_result = self.client.cancel_order(pending.order_id)
        if cancel_result.get("error"):
            if "404" in str(cancel_result):
                logger.info("Cancel returned 404 for %s — order may not exist, stopping chase", pending.order_id)
                self.state.log("warn", f"Cancel unavailable for this order — stopping chase")
                pending.chase_attempts = pending.max_chase_attempts
                return
            logger.debug("Failed to cancel order for chase: %s", cancel_result)
            return

        try:
            resp = self.client.place_limit_buy(
                market_id=market.market_id,
                token_id=pending.token_id,
                price=new_price,
                size_usdt=pending.stake,
                fee_rate_bps=pending.fee_rate_bps,
                is_neg_risk=False,
                is_yield_bearing=False,
            )
            new_order_id = str(resp.get("orderID", ""))
            if not new_order_id:
                logger.warning("Chase order did not return order id: %s", resp)
                return
            logger.info("Chased order %s -> %s @ %s (was @ %s)", pending.order_id, new_order_id, f"{new_price:.2%}", f"{pending.entry_price:.2%}")
            self.state.log("trade", f"[LIVE] CHASE ORDER {pending.side.upper()} @ {new_price:.0%} ${pending.stake:.2f} (was @ {pending.entry_price:.0%})")
            pending.order_id = new_order_id
            pending.entry_price = new_price
            pending.last_chase_monotonic = monotonic_now
            pending.chase_attempts += 1
            pending.taker_fee_rate = self.config.taker_fee_rate
        except Exception as exc:
            logger.warning("Failed to place chase order: %s", exc)

    def _build_account_snapshot(self) -> dict[str, Any]:
        with self._bankroll_lock:
            equity = self._equity
            initial = self._initial
            peak = self._peak

        paper_block = {
            "paper_equity": round(equity, 4),
            "paper_initial": initial,
            "paper_peak": round(peak, 4),
            "paper_return_pct": round((equity / initial - 1.0) * 100, 2) if initial else 0.0,
        }

        signer = None
        if self.config.private_key:
            key = self.config.private_key if self.config.private_key.startswith("0x") else f"0x{self.config.private_key}"
            try:
                signer = Account.from_key(key).address
            except Exception:
                signer = None

        return {
            "mode": self.config.trading_mode,
            "signer_address": signer,
            "funder_address": self.config.funder_address,
            "has_wallet": self.config.has_wallet,
            **paper_block,
        }