"""Main trading bot loop — Signull strategies on live markets (paper or real)."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from eth_account import Account

from strategies.base import CandleContext, TickContext
from strategies.signull_1_0 import Signull10Strategy
from strategies.signull_1_1 import Signull11Strategy
from strategies.signull_1_2 import Signull12Strategy

from .account import fetch_positions
from .config import BotConfig
from .markets import (
    CandleMarket,
    get_current_candle,
    market_to_dict,
    resolve_candle_winner,
    winner_from_price_refs,
    winner_from_ticks,
)
from .polymarket import PolymarketClient
from .sizing import (
    cap_stake_for_taker_fee,
    compute_stake,
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
    mode: str  # paper | live
    token_id: str
    order_id: str | None = None
    requested_shares: float = 0.0
    filled_shares: float = 0.0
    entry_fee: float = 0.0
    taker_fee_rate: float = 0.0


@dataclass
class _SettleResult:
    """Outcome of background resolution; applied on the bot thread."""

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
        self.client = PolymarketClient(config)

        if config.strategy_id == "signull_1_2":
            self.strategy = Signull12Strategy(config.strategy_params())
        elif config.strategy_id == "signull_1_1":
            self.strategy = Signull11Strategy(config.strategy_params())
        else:
            self.strategy = Signull10Strategy(
                config.strategy_params(),
                asset=config.asset,
            )
        self._initial = float(config.paper_initial_capital)
        self._equity = float(config.paper_initial_capital)
        self._peak = float(config.paper_initial_capital)
        self._wins_recent: list[bool] = []
        self._wins_streak = 0
        self._losses_streak = 0
        self._last_session_save = 0.0
        self._apply_session(session or load_session(config))
        # Serialize bankroll reads/writes; settle results applied on tick path.
        self._bankroll_lock = threading.Lock()
        self._settle_results: queue.Queue[_SettleResult] = queue.Queue()
        self._settling_slugs: set[str] = set()

        self._active_slug: str | None = None
        self._active_start_ts: int | None = None
        self._active_title: str = ""
        self._candle_ticks: list[tuple[int, float, float]] = []
        self._entered = False
        self._pending: PendingTrade | None = None
        self._heartbeat_id = ""
        self._last_account_refresh = 0.0
        self._cached_account: dict[str, Any] | None = None
        self._cached_open_orders: list[dict] = []
        self._cached_wallet_balance: float | None = None

        self.state.update(
            running=False,
            mode=config.trading_mode,
            asset=config.asset,
        )
        self._push_strategy_state(signal_side="hold", signal_reason="Starting…")

    def run(self) -> None:
        mode = "LIVE" if self.config.is_live else "PAPER"
        thr = self.strategy.params["threshold"]
        msg = (
            f"Bot started [{mode}] {self.strategy.meta.name} — {self.config.asset.upper()}, "
            f"limit @{thr:.0%}, paper bankroll ${self._initial:.2f}"
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
            self._initial = float(
                session.get("initial_capital", self._initial)
            )
            self._equity = float(session.get("equity", self._equity))
            self._peak = float(
                session.get("peak_equity", max(self._peak, self._equity))
            )

        self._wins_recent = [
            bool(value) for value in session.get("wins_recent", [])
        ][-10:]
        self._wins_streak = int(session.get("wins_streak", 0))
        self._losses_streak = int(session.get("losses_streak", 0))

        self.state.restore_persisted(
            strategy_trades=session.get("strategy_trades"),
            equity_history=session.get("equity_history"),
            trades_placed=int(session.get("trades_placed", 0)),
        )

        if not self.config.is_live:
            self.state.log(
                "info",
                f"Restored paper session — equity ${self._equity:.2f} "
                f"(initial ${self._initial:.2f})",
            )
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
                "strategy_trades": list(snap.get("strategy_trades", []))[
                    :MAX_STRATEGY_TRADES
                ],
                "equity_history": list(snap.get("equity_history", []))[
                    -MAX_EQUITY_HISTORY:
                ],
            }
        save_session(self.config, payload)
        self._last_session_save = time.time()

    def _tick(self) -> None:
        # Apply completed settlements before sizing / signals so equity is current.
        self._drain_settle_results()

        if self.config.is_live and self.client.is_authenticated:
            try:
                self._heartbeat_id = self.client.send_heartbeat(self._heartbeat_id)
            except Exception:
                logger.debug("Heartbeat failed", exc_info=True)

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

        # Cap tick buffer (enough for noise path + memory)
        if len(self._candle_ticks) > 2000:
            self._candle_ticks = self._candle_ticks[-1500:]

        ctx = CandleContext(
            slug=market.slug,
            title=market.title,
            start_ts=market.candle_start_ts,
            end_ts=int(market.end_date.timestamp()),
            winner="",
        )
        tick = TickContext(
            t=now_ts,
            up=up_mid,
            down=down_mid,
            seconds_into_candle=max(0.0, now_ts - market.candle_start_ts),
            seconds_to_close=max(0.0, market.seconds_to_close),
        )

        self._sync_account_to_strategy()

        signal = None
        if not self._entered:
            signal = self.strategy.evaluate(tick, ctx, entered=False)

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
        elif signal is not None:
            signal_side = signal.side
            signal_reason = signal.reason

        # Wallet endpoints are slow and are dashboard-only information.  Do
        # not put them on the signal path every polling cycle.
        if time.time() - self._last_account_refresh >= 5:
            self._cached_account = self._build_account_snapshot()
            self._cached_open_orders = (
                self.client.get_open_orders()
                if self.config.is_live and self.client.is_authenticated
                else []
            )
            if self.config.is_live and self.client.is_authenticated:
                bal = self.client.get_collateral_balance()
                if bal is not None:
                    self._cached_wallet_balance = bal
            self._last_account_refresh = time.time()
        account_data = self._cached_account or self._build_account_snapshot()
        open_orders = self._cached_open_orders

        self.state.update(
            last_tick_at=time.time(),
            last_error=None,
            market=market_to_dict(market),
            prices={"up": up_mid, "down": down_mid},
            signal={
                "side": signal_side,
                "price": signal.price if signal else (
                    self._pending.entry_price if self._pending else 0.0
                ),
                "reason": signal_reason,
            },
            account=account_data,
            open_orders=open_orders[:20],
            positions=account_data.get("positions", []) if account_data else [],
        )
        self.state.increment("ticks")
        self._push_strategy_state(
            signal_side=signal_side,
            signal_reason=signal_reason,
        )

        if signal is not None and not self._entered:
            self._enter(market, signal, tick, ctx)

        if time.time() - self._last_session_save >= 30.0:
            self.persist_session()

    def _waiting_message(self) -> str:
        thr = float(self.strategy.params["threshold"])
        if self.config.strategy_id == "signull_1_1":
            late = float(self.strategy.params.get("late_entry_seconds", 2.0))
            return (
                f"Watching for ≥ {thr:.0%} or end-of-candle entry "
                f"(last {late:.0f}s)…"
            )
        if thr < 0.5:
            return f"Waiting for a side to drop ≤ {thr:.0%}…"
        return f"Waiting for a side to reach ≥ {thr:.0%}…"

    def _snapshot_resolution_refs(self, start_ts: int | None) -> dict[str, float | None]:
        """Prefer frozen closed-window refs; fall back to live capture."""
        if start_ts is not None:
            frozen = self.state.get_frozen_resolution_refs(int(start_ts))
            if frozen is not None:
                return frozen
            # Freeze now if feed has not yet; captures current before we clear.
            return self.state.freeze_resolution_refs(int(start_ts))
        return self.state.get_resolution_refs()

    def _market_from_feed(self) -> CandleMarket | None:
        """Use the feed subscription's current candle before falling back to Gamma."""
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
            )
            return market if market.seconds_to_close > 0 else None
        except (KeyError, TypeError, ValueError):
            return None

    def _on_new_candle(self, market: CandleMarket) -> None:
        # Snapshot prior candle for background settle (never block the live loop)
        prev_slug = self._active_slug
        prev_title = self._active_title
        prev_start = self._active_start_ts
        prev_ticks = list(self._candle_ticks)
        prev_pending = self._pending

        # Prefer start_ts encoded in slug (authoritative)
        start_ts = prev_start
        if prev_slug is not None:
            try:
                start_ts = int(str(prev_slug).rsplit("-", 1)[-1])
            except ValueError:
                start_ts = prev_start or market.candle_start_ts - 300

        # CRITICAL: freeze beat/oracle for the candle that just closed *before*
        # clear_market_data(). Feed may already have frozen; first freeze wins.
        prev_refs = self._snapshot_resolution_refs(start_ts if prev_slug else None)

        self._active_slug = market.slug
        self._active_start_ts = market.candle_start_ts
        self._active_title = market.title
        self._candle_ticks = []
        self._entered = False
        self._pending = None

        if prev_slug is not None:
            self._schedule_candle_settlement(
                slug=prev_slug,
                title=prev_title or prev_slug,
                start_ts=int(start_ts),
                ticks=prev_ticks,
                pending=prev_pending,
                refs=prev_refs,
            )

        # These 1.0-only helpers maintain its BTC/noise cache. Simpler
        # strategies such as 1.1 intentionally do not need them.
        ensure_candle = getattr(self.strategy, "ensure_current_candle", None)
        if callable(ensure_candle):
            ensure_candle(market.slug)
        refresh_klines = getattr(self.strategy, "schedule_klines_refresh", None)
        if callable(refresh_klines):
            refresh_klines(int(time.time()))

        # Feed usually already switched this candle (cleared books + locked beat).
        # Re-clearing here wiped history and re-pinned beat to a *later* spot
        # price — Δ then sat near $0 for the rest of the candle. Only clear /
        # seed when the feed has not already landed on this slug.
        feed_m = self.state.get_market() or {}
        feed_already = (
            feed_m.get("slug") == market.slug
            and not feed_m.get("provisional")
            and feed_m.get("up_token_id")
        )
        if not feed_already:
            beat = self.state.get_btc_price()
            self.state.clear_market_data(
                closing_start_ts=start_ts if prev_slug else None
            )
            if beat is not None:
                self.state.set_price_to_beat(
                    beat, candle_start_ts=market.candle_start_ts
                )
        else:
            # Ensure open beat is locked for this window (no-op if feed set it).
            beat = self.state.get_btc_price()
            if beat is not None:
                self.state.set_price_to_beat(
                    beat, candle_start_ts=market.candle_start_ts
                )
        self.state.update(
            market=market_to_dict(market),
            prices=None,
            signal={"side": "hold", "reason": f"New candle — {self.strategy.meta.name} watching"},
        )
        msg = f"New candle: {market.title}"
        logger.info("%s (closes in %.0fs)", msg, market.seconds_to_close)
        self.state.log("info", msg)
        self._push_strategy_state(
            signal_side="hold",
            signal_reason="New candle — watching",
        )

    def _maybe_finalize_closing_candle(self, market: CandleMarket) -> None:
        """Start settlement as soon as the candle ends — don't wait for slug rollover."""
        if self._pending is None or self._active_slug is None:
            return
        if self._pending.slug != self._active_slug:
            return
        close_window = max(2.0, float(self.config.bot_poll_interval_sec) + 0.5)
        if market.seconds_to_close > close_window:
            return
        start_ts = self._active_start_ts or market.candle_start_ts
        self._schedule_candle_settlement(
            slug=self._active_slug,
            title=self._active_title or market.title,
            start_ts=int(start_ts),
            ticks=list(self._candle_ticks),
            pending=self._pending,
            refs=self._snapshot_resolution_refs(int(start_ts)),
        )

    def _schedule_candle_settlement(
        self,
        *,
        slug: str,
        title: str,
        start_ts: int,
        ticks: list[tuple[int, float, float]],
        pending: PendingTrade | None,
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
            kwargs={
                "slug": slug,
                "title": title,
                "start_ts": start_ts,
                "ticks": ticks,
                "pending": pending,
                "refs": refs,
            },
            daemon=True,
            name="signull-settle",
        ).start()

    def _finalize_candle(
        self,
        *,
        slug: str,
        title: str,
        start_ts: int,
        ticks: list[tuple[int, float, float]],
        pending: PendingTrade | None = None,
        refs: dict[str, float | None] | None = None,
    ) -> None:
        """Background: resolve winner / fill status; apply bankroll immediately."""
        try:
            noisy = self.strategy.register_closed_candle(slug, ticks)
            self.state.log(
                "info",
                f"Candle closed {slug[-12:]} · trust path {'NOISY' if noisy else 'clean'}",
            )

            if pending is None or pending.slug != slug:
                return

            filled = 0.0
            if pending.mode == "live":
                # A submitted GTC is not a fill.  Cancel anything still resting,
                # then record only the matched quantity.
                filled = self._close_live_order(pending)
                if filled <= 0:
                    self.state.log(
                        "info",
                        f"[LIVE] no fill for {pending.order_id or slug}; no trade settled",
                    )
                    return

            # Prefer frozen refs (survives feed clear) over the passed snapshot.
            frozen = self.state.get_frozen_resolution_refs(start_ts)
            use_refs = frozen if frozen is not None else (refs or {})

            winner, source = self._resolve_winner_reliable(
                start_ts=start_ts,
                ticks=ticks,
                refs=use_refs,
            )
            self._apply_settle_result(
                _SettleResult(
                    slug=slug,
                    title=title,
                    pending=pending,
                    winner=winner,
                    source=source,
                    filled_shares=filled,
                )
            )
        finally:
            with self._bankroll_lock:
                self._settling_slugs.discard(slug)

    def _drain_settle_results(self) -> None:
        """Apply bankroll mutations on the bot thread under the bankroll lock."""
        while True:
            try:
                result = self._settle_results.get_nowait()
            except queue.Empty:
                break
            self._apply_settle_result(result)

    def _apply_settle_result(self, result: _SettleResult) -> None:
        pending = result.pending
        if result.winner is None:
            self.state.log(
                "warn",
                f"Could not resolve winner for {result.slug} — voiding paper stake "
                f"(no PnL change)",
            )
            return

        # Scale stake to confirmed fill for live partials
        stake = pending.stake
        if pending.mode == "live" and result.filled_shares > 0:
            stake, _shares = scale_pending_for_fill(
                pending.stake,
                pending.requested_shares,
                result.filled_shares,
                pending.entry_price,
            )
            pending.stake = stake
            pending.filled_shares = result.filled_shares
            pending.entry_fee = estimate_taker_fee(
                stake, pending.entry_price, pending.taker_fee_rate
            )

        won = pending.side == result.winner
        pnl = _settle_pnl(
            stake, pending.entry_price, won, pending.entry_fee
        )
        self.strategy.on_trade_settled(won)

        with self._bankroll_lock:
            if pending.mode == "paper":
                self._equity += pnl
                self._peak = max(self._peak, self._equity)
                self._losses_streak = 0 if won else self._losses_streak + 1
                self._wins_recent.append(won)
                if len(self._wins_recent) > 10:
                    self._wins_recent.pop(0)
            # A confirmed live settlement is as valid as a paper settlement
            # for the strategy's consecutive-win sizing rule.
            self._wins_streak = self._wins_streak + 1 if won else 0
            equity_after = self._equity

        trade_rec = {
            "t": time.time(),
            "slug": result.slug,
            "title": result.title,
            "side": pending.side,
            "entry_price": pending.entry_price,
            "stake": round(stake, 4),
            "entry_fee": round(pending.entry_fee, 4),
            "size_label": pending.size_label,
            "risk_pct": pending.risk_pct,
            "winner": result.winner,
            "won": won,
            "pnl": round(pnl, 4) if pending.mode == "paper" else None,
            "equity_after": round(equity_after, 4) if pending.mode == "paper" else None,
            "mode": pending.mode,
            "reason": pending.reason,
            "resolve_source": result.source,
            "filled_shares": result.filled_shares if pending.mode == "live" else None,
        }
        self.state.record_strategy_trade(trade_rec)
        self.state.increment("trades_placed")

        if pending.mode == "paper":
            self.state.record_equity_point(
                equity_after,
                mode=self.config.trading_mode,
                force=True,
            )
        if self._pending is not None and self._pending.slug == result.slug:
            self._pending = None

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
        self.state.update(
            account=self._cached_account,
            signal={"side": "hold", "reason": msg},
        )
        self._push_strategy_state(
            signal_side="hold",
            signal_reason=msg,
        )

    def _close_live_order(self, pending: PendingTrade) -> float:
        """Cancel a resting order and return confirmed matched shares."""
        if not pending.order_id:
            return 0.0
        try:
            order = self.client.get_order(pending.order_id)
            for key in ("size_matched", "matched_size", "filled_size", "filled"):
                if order.get(key) is not None:
                    pending.filled_shares = float(order[key])
                    break
            status = str(order.get("status", "")).lower()
            if status not in {"matched", "filled", "cancelled", "canceled", "expired"}:
                self.client.cancel_order(pending.order_id)
                order = self.client.get_order(pending.order_id)
                for key in ("size_matched", "matched_size", "filled_size", "filled"):
                    if order.get(key) is not None:
                        pending.filled_shares = float(order[key])
                        break
        except Exception:
            logger.exception("Unable to reconcile/cancel live order %s", pending.order_id)
        return pending.filled_shares

    def _resolve_winner_reliable(
        self,
        *,
        start_ts: int,
        ticks: list[tuple[int, float, float]],
        refs: dict[str, float | None],
    ) -> tuple[str | None, str]:
        """
        Resolve instantly from frozen oracle data, then local ticks, then Gamma.
        """
        winner, source = winner_from_price_refs(refs)
        if winner is not None:
            return winner, source

        tick_winner = winner_from_ticks(ticks, at_close=True)
        if tick_winner is not None:
            return tick_winner, "ticks"

        for attempt in range(3):
            winner = resolve_candle_winner(
                self.config.asset, start_ts, require_resolved=True
            )
            if winner is not None:
                return winner, "gamma"
            if attempt < 2:
                time.sleep(0.15)

        return None, "none"

    def _enter(
        self,
        market: CandleMarket,
        signal,
        tick: TickContext,
        ctx: CandleContext,
    ) -> None:
        with self._bankroll_lock:
            equity = self._equity
            initial = self._initial

        token_id = (
            market.up_token_id if signal.side == "up" else market.down_token_id
        )
        mode = "live" if self.config.is_live else "paper"
        entry_price = float(signal.price)
        if self.config.is_live:
            # A midpoint or last print is not executable. Resolve the current
            # ask just before submitting instead of placing a stale 70¢ bid.
            best_ask = self.client.get_best_ask(token_id)
            if best_ask is None or not 0 < best_ask < 1:
                self.state.log("warn", "No executable ask — skipping strategy entry")
                return
            entry_price = float(best_ask)
            # Fee-aware strategies must size from the actual executable quote,
            # not the earlier midpoint/last-price trigger observation.
            signal.price = entry_price

        risk_frac = self.strategy.position_risk_fraction(signal, tick, ctx)
        size_label = self.strategy.size_label(risk_frac)

        wallet_balance = self._cached_wallet_balance
        if self.config.is_live and wallet_balance is None and self.client.is_authenticated:
            try:
                wallet_balance = self.client.get_collateral_balance()
                if wallet_balance is not None:
                    self._cached_wallet_balance = wallet_balance
            except Exception:
                logger.debug("wallet balance read failed", exc_info=True)

        stake = compute_stake(
            risk_frac,
            initial,
            equity,
            wallet_balance=wallet_balance,
            is_live=self.config.is_live,
        )
        available_cash = equity
        if self.config.is_live and wallet_balance is not None:
            available_cash = min(available_cash, wallet_balance)
        fee_rate = max(0.0, float(signal.taker_fee_rate))
        stake = cap_stake_for_taker_fee(
            stake, entry_price, fee_rate, available_cash
        )
        if stake < 0.01:
            self.state.log("warn", "Stake too small — skipping")
            self._entered = True
            return

        entry_fee = estimate_taker_fee(stake, entry_price, fee_rate)
        order_id: str | None = None

        if self.config.is_live:
            if not self.client.is_authenticated:
                self.state.log("error", "Live mode but wallet not authenticated")
                return
            try:
                resp = self.client.place_limit_buy(
                    token_id=token_id,
                    price=entry_price,
                    size_usdc=stake,
                    tick_size=market.tick_size,
                )
                order_id = str(
                    resp.get("orderID") or resp.get("order_id") or resp.get("id") or ""
                )
                if not order_id:
                    raise RuntimeError(f"CLOB did not return an order id: {resp}")
                logger.info("Live order: %s", resp)
                self.state.log(
                    "trade",
                    f"[LIVE] LIMIT BUY {signal.side.upper()} @ {entry_price:.0%} "
                    f"${stake:.2f} ({size_label})",
                )
            except Exception as exc:
                self.state.log("error", f"Order failed: {exc}")
                logger.exception("place_limit_buy failed")
                return
        else:
            # Paper: assume limit fills at threshold when signal fires
            self.state.log(
                "paper",
                f"[PAPER] FILL {signal.side.upper()} @ {entry_price:.0%} "
                f"${stake:.2f} ({size_label}) · {signal.reason}",
            )
            logger.info(
                "[PAPER] %s @ %.2f stake=%.2f %s",
                signal.side,
                entry_price,
                stake,
                size_label,
            )

        self._pending = PendingTrade(
            slug=market.slug,
            title=market.title,
            side=signal.side,
            entry_price=entry_price,
            stake=stake,
            size_label=size_label,
            risk_pct=round(risk_frac * 100, 2),
            reason=signal.reason,
            entry_ts=time.time(),
            start_ts=market.candle_start_ts,
            mode=mode,
            token_id=token_id,
            order_id=order_id,
            requested_shares=stake / entry_price if entry_price > 0 else 0.0,
            entry_fee=entry_fee,
            taker_fee_rate=fee_rate,
        )
        self._entered = True
        self._push_strategy_state(
            signal_side=signal.side,
            signal_reason=signal.reason,
        )

    def _sync_account_to_strategy(self) -> None:
        with self._bankroll_lock:
            equity = self._equity
            initial = self._initial
            peak = self._peak
            wins = sum(self._wins_recent)
            wins_streak = self._wins_streak
            losses = self._losses_streak
        self.strategy.on_account_update(
            equity,
            initial,
            peak,
            wins_recent=wins,
            wins_streak=wins_streak,
            losses_streak=losses,
            equity_momentum=0.0,
        )

    def _push_strategy_state(
        self,
        *,
        signal_side: str,
        signal_reason: str,
    ) -> None:
        pending = None
        if self._pending is not None:
            p = self._pending
            pending = {
                "side": p.side,
                "entry_price": p.entry_price,
                "stake": p.stake,
                "size_label": p.size_label,
                "risk_pct": p.risk_pct,
                "mode": p.mode,
                "slug": p.slug,
            }
        with self._bankroll_lock:
            equity = self._equity
            initial = self._initial
            peak = self._peak
            losses = self._losses_streak
            wins = sum(self._wins_recent)
        self.state.update(
            strategy={
                "id": self.strategy.meta.id,
                "name": self.strategy.meta.name,
                "mode": self.config.trading_mode,
                "params": dict(self.strategy.params),
                "equity": round(equity, 4),
                "initial": initial,
                "peak": round(peak, 4),
                "return_pct": round(
                    (equity / initial - 1.0) * 100, 2
                )
                if initial
                else 0.0,
                "pending": pending,
                "entered_this_candle": self._entered,
                "signal_side": signal_side,
                "signal_reason": signal_reason,
                "losses_streak": losses,
                "wins_recent": wins,
            }
        )

    def _read_prices(self, market: CandleMarket) -> tuple[float, float]:
        live = self.state.get_live_prices()
        if live and "up" in live and "down" in live:
            return float(live["up"]), float(live["down"])
        try:
            up_mid = self.client.get_midpoint(market.up_token_id)
            down_mid = self.client.get_midpoint(market.down_token_id)
            return float(up_mid), float(down_mid)
        except Exception:
            return float(market.up_price), float(market.down_price)

    def _refresh_books_rest(self, market: CandleMarket) -> None:
        from .feed import _normalize_levels

        for token_id, side in (
            (market.up_token_id, "up"),
            (market.down_token_id, "down"),
        ):
            try:
                book = self.client.get_order_book(token_id)
                bids = _normalize_levels(getattr(book, "bids", []) or [])
                asks = _normalize_levels(getattr(book, "asks", []) or [])
                if bids or asks:
                    self.state.update_feed_book(side, bids, asks)
            except Exception:
                logger.debug("REST book refresh failed for %s", side, exc_info=True)

    def _build_account_snapshot(self) -> dict[str, Any]:
        with self._bankroll_lock:
            equity = self._equity
            initial = self._initial
            peak = self._peak

        paper_block = {
            "paper_equity": round(equity, 4),
            "paper_initial": initial,
            "paper_peak": round(peak, 4),
            "paper_return_pct": round(
                (equity / initial - 1.0) * 100, 2
            )
            if initial
            else 0.0,
        }

        # A paper account must stay entirely local, even when wallet settings
        # are present.  This also prevents an invalid/stale private key from
        # affecting dashboard startup in paper mode.
        if not self.config.is_live:
            return {
                "connected": False,
                "mode": self.config.trading_mode,
                "balance_usdc": equity,
                "tips": [
                    (
                        f"PAPER: {self.strategy.meta.name} on live market prices "
                        f"(session persists across restarts)"
                    ),
                    "Add PRIVATE_KEY + FUNDER_ADDRESS for live orders",
                    "Run: python scripts/verify_wallet.py",
                ],
                **paper_block,
            }

        signer = None
        if self.config.private_key:
            key = (
                self.config.private_key
                if self.config.private_key.startswith("0x")
                else f"0x{self.config.private_key}"
            )
            signer = Account.from_key(key).address

        balance = None
        positions: list[dict] = []
        if self.client.is_authenticated:
            balance = self.client.get_collateral_balance()
            if balance is not None:
                self._cached_wallet_balance = balance
            try:
                positions = fetch_positions(self.config.funder_address)[:10]
            except Exception:
                logger.exception("Failed to fetch positions")

        return {
            "connected": self.client.is_authenticated,
            "signer_address": signer,
            "funder_address": self.config.funder_address,
            "signature_type": self.config.signature_type,
            "signature_label": self.config.signature_label,
            "balance_usdc": balance if self.config.is_live else equity,
            "positions": positions,
            "mode": self.config.trading_mode,
            "tips": (
                [f"CLOB authentication unavailable: {self.client.auth_error}"]
                if self.client.auth_error
                else []
            ),
            **paper_block,
        }
