"""Main trading bot loop — Signull 1.0 on live markets (paper or real)."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from eth_account import Account

from strategies.base import CandleContext, TickContext
from strategies.signull_1_0 import Signull10Strategy

from .account import fetch_positions
from .config import BotConfig
from .markets import (
    CandleMarket,
    get_current_candle,
    market_to_dict,
    resolve_candle_winner,
    winner_from_ticks,
)
from .polymarket import PolymarketClient
from .state import BotState

logger = logging.getLogger(__name__)


def _settle_pnl(stake: float, entry_price: float, won: bool) -> float:
    if entry_price <= 0 or stake <= 0:
        return 0.0
    if won:
        return stake / entry_price - stake
    return -stake


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


class TradingBot:
    def __init__(self, config: BotConfig, state: BotState | None = None):
        self.config = config
        self.state = state or BotState()
        self.client = PolymarketClient(config)

        self.strategy = Signull10Strategy(config.strategy_params())
        self._initial = float(config.paper_initial_capital)
        self._equity = float(config.paper_initial_capital)
        self._peak = float(config.paper_initial_capital)
        self._wins_recent: list[bool] = []
        self._losses_streak = 0

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
            f"Bot started [{mode}] Signull 1.0 — {self.config.asset.upper()}, "
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

    def _tick(self) -> None:
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

        thr = float(self.strategy.params["threshold"])
        if thr < 0.5:
            wait_msg = f"Waiting for a side to drop ≤ {thr:.0%}…"
        else:
            wait_msg = f"Waiting for a side to reach ≥ {thr:.0%}…"

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
            self._cached_open_orders = self.client.get_open_orders() if self.config.has_wallet else []
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

    def _snapshot_resolution_refs(self) -> dict[str, float | None]:
        """Capture beat/oracle *before* market data is cleared for the next candle."""
        beat = None
        chainlink = None
        spot = None
        try:
            snap = self.state.get_snapshot(history_points=0)
            btc = snap.get("btc") or {}
            beat = btc.get("price_to_beat")
            chainlink = btc.get("chainlink")
            spot = btc.get("price")
        except Exception:
            pass
        return {
            "beat": float(beat) if beat is not None else None,
            "chainlink": float(chainlink) if chainlink is not None else None,
            "spot": float(spot) if spot is not None else None,
        }

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
        # CRITICAL: capture beat/oracle for the candle that just closed.
        # clear_market_data() resets price_to_beat for the *new* candle — using
        # live state after that mis-resolves winners (e.g. bought Up, closed Up,
        # but settled as LOSS against the next open).
        prev_refs = self._snapshot_resolution_refs()

        self._active_slug = market.slug
        self._active_start_ts = market.candle_start_ts
        self._active_title = market.title
        self._candle_ticks = []
        self._entered = False
        self._pending = None

        if prev_slug is not None:
            # Prefer start_ts encoded in slug (authoritative)
            start_ts = prev_start
            try:
                start_ts = int(str(prev_slug).rsplit("-", 1)[-1])
            except ValueError:
                start_ts = prev_start or market.candle_start_ts - 300

            threading.Thread(
                target=self._finalize_candle,
                kwargs={
                    "slug": prev_slug,
                    "title": prev_title,
                    "start_ts": int(start_ts),
                    "ticks": prev_ticks,
                    "pending": prev_pending,
                    "refs": prev_refs,
                },
                daemon=True,
                name="signull-settle",
            ).start()

        self.strategy.ensure_current_candle(market.slug)
        self.strategy.refresh_btc_klines(int(time.time()))

        beat = self.state.get_btc_price()
        self.state.clear_market_data()
        if beat is not None:
            self.state.set_price_to_beat(beat)
        self.state.update(
            market=market_to_dict(market),
            prices=None,
            signal={"side": "hold", "reason": "New candle — Signull 1.0 watching"},
        )
        msg = f"New candle: {market.title}"
        logger.info("%s (closes in %.0fs)", msg, market.seconds_to_close)
        self.state.log("info", msg)
        self._push_strategy_state(
            signal_side="hold",
            signal_reason="New candle — watching",
        )

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
        noisy = self.strategy.register_closed_candle(slug, ticks)
        self.state.log(
            "info",
            f"Candle closed {slug[-12:]} · trust path {'NOISY' if noisy else 'clean'}",
        )

        if pending is None or pending.slug != slug:
            return

        if pending.mode == "live":
            # A submitted GTC is not a fill.  Cancel anything still resting,
            # then record only the matched quantity.  Settlement/PnL remains
            # wallet-authoritative and is never fabricated from the paper book.
            filled = self._close_live_order(pending)
            if filled <= 0:
                self.state.log("info", f"[LIVE] no fill for {pending.order_id or slug}; no trade settled")
                return

        winner, source = self._resolve_winner_reliable(
            start_ts=start_ts,
            ticks=ticks,
            refs=refs or {},
        )
        if winner is None:
            self.state.log(
                "warn",
                f"Could not resolve winner for {slug} — voiding paper stake "
                f"(no PnL change)",
            )
            return

        won = pending.side == winner
        pnl = _settle_pnl(pending.stake, pending.entry_price, won)
        if pending.mode == "paper":
            self._equity += pnl
        if pending.mode == "paper":
            self._peak = max(self._peak, self._equity)
            self._losses_streak = 0 if won else self._losses_streak + 1
            self._wins_recent.append(won)
            if len(self._wins_recent) > 10:
                self._wins_recent.pop(0)

        trade_rec = {
            "t": time.time(),
            "slug": slug,
            "title": title,
            "side": pending.side,
            "entry_price": pending.entry_price,
            "stake": round(pending.stake, 4),
            "size_label": pending.size_label,
            "risk_pct": pending.risk_pct,
            "winner": winner,
            "won": won,
            "pnl": round(pnl, 4) if pending.mode == "paper" else None,
            "equity_after": round(self._equity, 4) if pending.mode == "paper" else None,
            "mode": pending.mode,
            "reason": pending.reason,
            "resolve_source": source,
        }
        self.state.record_strategy_trade(trade_rec)
        self.state.increment("trades_placed")

        result = "WIN" if won else "LOSS"
        msg = (
            f"[{pending.mode.upper()}] {result} {pending.side.upper()} "
            f"(winner={winner} via {source}) "
            f"@ {pending.entry_price:.0%} stake ${pending.stake:.2f} "
            f"pnl {pnl:+.2f} → equity ${self._equity:.2f}"
        )
        if pending.mode == "live":
            msg = (
                f"[LIVE] {result} {pending.side.upper()} (winner={winner} via {source}) "
                f"confirmed fill {filled:.2f} shares; wallet settlement pending"
            )
        self.state.log("trade" if won else "warn", msg)
        logger.info(msg)
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
        Resolve in order of trust:
          1. Gamma strict settlement (retry — prices lag a few seconds)
          2. End-of-candle mids from our own tick path (≥90¢)
          3. Snapshotted Chainlink/spot vs price-to-beat for *this* candle only
        """
        # 1) Wait for real market resolution (do not trust half-resolved mids)
        for attempt in range(12):
            winner = resolve_candle_winner(
                self.config.asset, start_ts, require_resolved=True
            )
            if winner is not None:
                return winner, "gamma"
            time.sleep(0.75 if attempt < 4 else 1.25)

        # 2) Our last observed mids on this candle
        tick_winner = winner_from_ticks(ticks)
        if tick_winner is not None:
            return tick_winner, "ticks"

        # 3) BTC path for *this* candle only (snapshotted before rollover)
        beat = refs.get("beat")
        ref = refs.get("chainlink")
        if ref is None:
            ref = refs.get("spot")
        if beat is not None and ref is not None:
            winner = "up" if float(ref) >= float(beat) else "down"
            return winner, f"btc(beat={beat:.2f},ref={ref:.2f})"

        return None, "none"

    def _enter(
        self,
        market: CandleMarket,
        signal,
        tick: TickContext,
        ctx: CandleContext,
    ) -> None:
        risk_frac = self.strategy.position_risk_fraction(signal, tick, ctx)
        size_label = self.strategy.size_label(risk_frac)
        stake = min(self._initial * risk_frac, self._equity)
        if stake < 0.01:
            self.state.log("warn", "Stake too small — skipping")
            self._entered = True
            return

        entry_price = float(signal.price)
        token_id = (
            market.up_token_id if signal.side == "up" else market.down_token_id
        )
        mode = "live" if self.config.is_live else "paper"

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
                order_id = str(resp.get("orderID") or resp.get("order_id") or resp.get("id") or "")
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
            order_id=order_id if self.config.is_live else None,
            requested_shares=stake / entry_price,
        )
        self._entered = True
        self._push_strategy_state(
            signal_side=signal.side,
            signal_reason=signal.reason,
        )

    def _sync_account_to_strategy(self) -> None:
        self.strategy.on_account_update(
            self._equity,
            self._initial,
            self._peak,
            wins_recent=sum(self._wins_recent),
            losses_streak=self._losses_streak,
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
        self.state.update(
            strategy={
                "id": "signull_1_0",
                "name": "Signull 1.0",
                "mode": self.config.trading_mode,
                "params": dict(self.strategy.params),
                "equity": round(self._equity, 4),
                "initial": self._initial,
                "peak": round(self._peak, 4),
                "return_pct": round(
                    (self._equity / self._initial - 1.0) * 100, 2
                )
                if self._initial
                else 0.0,
                "pending": pending,
                "entered_this_candle": self._entered,
                "signal_side": signal_side,
                "signal_reason": signal_reason,
                "losses_streak": self._losses_streak,
                "wins_recent": sum(self._wins_recent),
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
        paper_block = {
            "paper_equity": round(self._equity, 4),
            "paper_initial": self._initial,
            "paper_peak": round(self._peak, 4),
            "paper_return_pct": round(
                (self._equity / self._initial - 1.0) * 100, 2
            )
            if self._initial
            else 0.0,
        }

        if not self.config.has_wallet:
            return {
                "connected": False,
                "mode": self.config.trading_mode,
                "balance_usdc": self._equity if not self.config.is_live else None,
                "tips": [
                    "PAPER: Signull 1.0 simulates fills at threshold on live prices",
                    "Add PRIVATE_KEY + FUNDER_ADDRESS for live orders",
                    "Run: python scripts/verify_wallet.py",
                ],
                **paper_block,
            }

        signer = Account.from_key(
            self.config.private_key
            if self.config.private_key.startswith("0x")
            else f"0x{self.config.private_key}"
        ).address

        balance = None
        positions: list[dict] = []
        if self.client.is_authenticated:
            balance = self.client.get_collateral_balance()
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
            "balance_usdc": balance if self.config.is_live else self._equity,
            "positions": positions,
            "mode": self.config.trading_mode,
            **paper_block,
        }
