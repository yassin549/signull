"""Local FastAPI server + real-time WebSocket dashboard."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .account import verify_wallet
from .bot import TradingBot
from .config import BotConfig
from .btc_feed import BtcPriceFeed
from .feed import MarketFeed
from .session_store import load_session
from .state import BotState

logger = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


class BroadcastHub:
    """Push state to all dashboard clients as fast as the feed updates."""

    def __init__(self, state: BotState, push_interval_ms: int = 50):
        self.state = state
        self.push_interval = push_interval_ms / 1000
        self.clients: set[WebSocket] = set()
        self._last_version = -1

    def add(self, ws: WebSocket) -> None:
        self.clients.add(ws)

    def remove(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def run(self) -> None:
        while True:
            version = self.state.version
            if version != self._last_version and self.clients:
                self._last_version = version
                # The browser retains history between messages; a short tail is
                # enough for recovery and avoids serializing thousands of chart
                # points for every market update/client.
                # ~45s tail at 20 Hz for merge recovery without huge payloads.
                payload = self.state.get_snapshot(history_points=900)
                dead: list[WebSocket] = []
                for ws in list(self.clients):
                    try:
                        await ws.send_json(payload)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    self.remove(ws)
            await asyncio.sleep(self.push_interval)


class BotService:
    def __init__(self, config: BotConfig):
        self.config = config
        self.state = BotState()
        session = load_session(config)
        if session:
            self.state.restore_persisted(
                strategy_trades=session.get("strategy_trades"),
                equity_history=session.get("equity_history"),
                trades_placed=int(session.get("trades_placed", 0)),
            )
        self.hub = BroadcastHub(self.state, config.dashboard_push_ms)
        self.feed = MarketFeed(config, self.state)
        self.btc_feed = BtcPriceFeed(self.state, asset=config.asset)
        self._bot: TradingBot | None = None
        self._thread: threading.Thread | None = None
        self._feed_task: asyncio.Task | None = None
        self._btc_feed_task: asyncio.Task | None = None
        self._hub_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start_bot(self) -> None:
        if self.is_running:
            return
        self.state.clear_stop()
        self._bot = TradingBot(
            self.config,
            self.state,
            session=load_session(self.config),
        )
        self._thread = threading.Thread(target=self._bot.run, daemon=True)
        self._thread.start()

    def stop_bot(self) -> None:
        self.state.request_bot_stop()

    async def ensure_feed(self) -> None:
        if self._feed_task is None or self._feed_task.done():
            logger.info("Starting market feed")
            self._feed_task = asyncio.create_task(self.feed.run())

    async def ensure_btc_feed(self) -> None:
        if self._btc_feed_task is None or self._btc_feed_task.done():
            logger.info("Starting BTC price feed")
            self._btc_feed_task = asyncio.create_task(self.btc_feed.run())

    async def feed_watchdog(self) -> None:
        while not self.state.should_shutdown():
            await self.ensure_feed()
            await self.ensure_btc_feed()
            await asyncio.sleep(5)

    async def start_hub(self) -> None:
        if self._hub_task is None or self._hub_task.done():
            self._hub_task = asyncio.create_task(self.hub.run())

    async def shutdown_background_tasks(self) -> None:
        """Cancel and join service tasks so Uvicorn can exit cleanly."""
        tasks = [
            task
            for task in (
                self._watchdog_task,
                self._feed_task,
                self._btc_feed_task,
                self._hub_task,
            )
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.feed.close()


def create_app(config: BotConfig) -> FastAPI:
    service = BotService(config)
    app = FastAPI(title="Signull", version="0.2.0")

    if DASHBOARD_DIR.exists():
        app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(DASHBOARD_DIR / "index.html")

    @app.get("/api/status")
    async def status():
        snap = service.state.get_snapshot(history_points=6000)
        snap["bot_thread_alive"] = service.is_running
        return snap

    @app.get("/api/config")
    async def get_config():
        return {
            "trading_mode": config.trading_mode,
            "asset": config.asset,
            "order_size_usdc": config.order_size_usdc,
            "max_entry_price": config.max_entry_price,
            "dashboard_push_ms": config.dashboard_push_ms,
            "bot_poll_interval_sec": config.bot_poll_interval_sec,
            "has_wallet": config.has_wallet,
            "signature_type": config.signature_type,
            "signature_label": config.signature_label,
            "strategy": config.strategy_id,
            "strategy_name": (
                service._bot.strategy.meta.name
                if service._bot is not None
                else config.strategy_id
            ),
            "paper_initial_capital": config.paper_initial_capital,
            "strategy_params": config.strategy_params(),
        }

    @app.get("/api/wallet/verify")
    async def wallet_verify():
        return verify_wallet(config).to_dict()

    @app.post("/api/bot/start")
    async def bot_start():
        service.start_bot()
        return {"ok": True, "running": service.is_running}

    @app.post("/api/bot/stop")
    async def bot_stop():
        service.stop_bot()
        return {"ok": True, "running": service.is_running}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        service.hub.add(ws)
        try:
            # Send immediate full snapshot on connect
            await ws.send_json(service.state.get_snapshot(history_points=6000))
            while True:
                await asyncio.sleep(60)
        except (WebSocketDisconnect, asyncio.CancelledError):
            # Normal disconnect or application shutdown. CancelledError is a
            # BaseException in modern Python, so it is not covered below.
            pass
        except Exception:
            pass
        finally:
            service.hub.remove(ws)

    @app.on_event("startup")
    async def startup():
        service.state.clear_bot_stop()
        service.start_bot()
        await service.ensure_feed()
        await service.ensure_btc_feed()
        await service.start_hub()
        service._watchdog_task = asyncio.create_task(service.feed_watchdog())

    @app.on_event("shutdown")
    async def shutdown():
        service.state.request_shutdown()
        service.stop_bot()
        if service._thread is not None:
            service._thread.join(timeout=5.0)
        if service._bot is not None:
            service._bot.persist_session()
        await service.shutdown_background_tasks()

    return app
