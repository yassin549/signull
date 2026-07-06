"""Main trading bot loop."""

from __future__ import annotations

import logging
import time
from eth_account import Account

from .account import fetch_positions, verify_wallet
from .config import BotConfig
from .markets import CandleMarket, get_current_candle, market_to_dict
from .polymarket import PolymarketClient
from .state import BotState
from .strategy import Side, momentum_signal

logger = logging.getLogger(__name__)


class TradingBot:
    def __init__(self, config: BotConfig, state: BotState | None = None):
        self.config = config
        self.state = state or BotState()
        self.client = PolymarketClient(config)
        self._active_slug: str | None = None
        self._traded_this_candle = False
        self._heartbeat_id = ""

        self.state.update(
            running=False,
            mode=config.trading_mode,
            asset=config.asset,
        )

    def run(self) -> None:
        mode = "LIVE" if self.config.is_live else "PAPER"
        msg = (
            f"Bot started [{mode}] — {self.config.asset.upper()}, "
            f"${self.config.order_size_usdc:.2f}/trade"
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
        if self.config.is_live:
            self._heartbeat_id = self.client.send_heartbeat(self._heartbeat_id)

        market = get_current_candle(self.config.asset)
        if market is None:
            self.state.log("warn", f"No active {self.config.asset.upper()} 5M candle")
            return

        if market.slug != self._active_slug:
            self._active_slug = market.slug
            self._traded_this_candle = False
            beat = self.state.get_btc_price()
            self.state.clear_market_data()
            if beat is not None:
                self.state.set_price_to_beat(beat)
            self.state.update(
                market=market_to_dict(market),
                prices=None,
                signal={"side": "hold", "reason": "New candle — warming up"},
            )
            msg = f"New candle: {market.title}"
            logger.info("%s (closes in %.0fs)", msg, market.seconds_to_close)
            self.state.log("info", msg)

        if not self.state.is_feed_connected():
            self._refresh_books_rest(market)

        live = self.state.get_live_prices()
        if live and "up" in live and "down" in live:
            up_mid = live["up"]
            down_mid = live["down"]
        else:
            up_mid = self.client.get_midpoint(market.up_token_id)
            down_mid = self.client.get_midpoint(market.down_token_id)

        signal = momentum_signal(
            market,
            up_mid,
            down_mid,
            self.config.max_entry_price,
        )

        account_data = self._build_account_snapshot()
        open_orders = self.client.get_open_orders() if self.config.has_wallet else []

        self.state.update(
            last_tick_at=time.time(),
            last_error=None,
            market=market_to_dict(market),
            prices={"up": up_mid, "down": down_mid},
            signal={
                "side": signal.side.value,
                "price": signal.price,
                "reason": signal.reason,
            },
            account=account_data,
            open_orders=open_orders[:20],
            positions=account_data.get("positions", []) if account_data else [],
        )
        self.state.increment("ticks")

        if signal.side != Side.HOLD and not self._traded_this_candle:
            self._execute(market, signal)
            self._traded_this_candle = True

    def _refresh_books_rest(self, market: CandleMarket) -> None:
        from .feed import _normalize_levels

        for token_id, side in ((market.up_token_id, "up"), (market.down_token_id, "down")):
            try:
                book = self.client.get_order_book(token_id)
                bids = _normalize_levels(getattr(book, "bids", []) or [])
                asks = _normalize_levels(getattr(book, "asks", []) or [])
                if bids or asks:
                    self.state.update_feed_book(side, bids, asks)
            except Exception:
                logger.debug("REST book refresh failed for %s", side, exc_info=True)

    def _build_account_snapshot(self) -> dict | None:
        if not self.config.has_wallet:
            return {
                "connected": False,
                "mode": self.config.trading_mode,
                "tips": [
                    "Add PRIVATE_KEY and FUNDER_ADDRESS to .env",
                    "Run: python scripts/verify_wallet.py",
                ],
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
            "balance_usdc": balance,
            "positions": positions,
            "mode": self.config.trading_mode,
        }

    def _execute(self, market: CandleMarket, signal) -> None:
        if self.config.is_live:
            resp = self.client.place_limit_buy(
                token_id=signal.token_id,
                price=signal.price,
                size_usdc=self.config.order_size_usdc,
                tick_size=market.tick_size,
            )
            self.state.increment("trades_placed")
            self.state.log("trade", f"BUY {signal.side.value.upper()} @ ${signal.price:.3f}")
            logger.info("Order placed: %s", resp)
        else:
            msg = (
                f"[PAPER] BUY {signal.side.value.upper()} @ ${signal.price:.3f} "
                f"(${self.config.order_size_usdc:.2f})"
            )
            self.state.log("paper", msg)
            logger.info(msg)