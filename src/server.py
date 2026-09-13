"""Local FastAPI server + real-time WebSocket dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .account import verify_wallet
from .ai_settings import public_ai_settings, save_ai_settings
from .backtest_server import register_backtest_routes
from .bot import TradingBot
from .config import BotConfig
from .btc_feed import BtcPriceFeed
from .feed import MarketFeed
from .health import HealthMonitor
from .openrouter import OpenRouterError, list_models
from .backtest.registry import get_strategy
from .session_store import clear_session, load_session
from .state import BotState
from .wallet_monitor import WalletMonitor

logger = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"


class StrategyUpdateRequest(BaseModel):
    strategy_id: str
    params: dict | None = None


class CancelOrderRequest(BaseModel):
    order_id: str


class AISettingsRequest(BaseModel):
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    clear_key: bool = False


class BroadcastHub:
    """Push state to all dashboard clients as fast as the feed updates.

    Full-snapshot pushes are expensive (serialising history for every client).
    This hub sends delta-only pushes at high frequency and full snapshots at
    a much lower cadence, dramatically reducing payload size.
    """

    def __init__(self, state: BotState, push_interval_ms: int = 50):
        self.state = state
        self.push_interval = push_interval_ms / 1000
        self.clients: set[WebSocket] = set()
        self._last_version = -1
        self._last_full_broadcast = 0.0
        self._full_broadcast_interval = 2.0  # full snapshot every 2s is enough
        self._prev_snapshot: dict = {}

    def add(self, ws: WebSocket) -> None:
        self.clients.add(ws)

    def remove(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    def _build_delta(self, full: dict) -> dict:
        """Return only the fields that changed since the last push."""
        now = time.time()
        delta: dict[str, any] = {"t": now}

        # Always include version so the client can detect changes
        delta["version"] = full.get("version")

        # Small / fast-changing fields — include on every push
        for key in ("prices", "signal", "mode", "asset", "running", "last_tick_at", "trades_placed"):
            prev = self._prev_snapshot.get(key)
            cur = full.get(key)
            if cur != prev:
                delta[key] = cur

        # BTC panel — always include (compact and fast-changing)
        delta["btc"] = full.get("btc")

        # Feed status — always include
        delta["feed"] = full.get("feed")

        # Health — include every push (small payload)
        delta["health"] = full.get("health")

        # Strategy state — include when changed
        strat = full.get("strategy")
        prev_strat = self._prev_snapshot.get("strategy")
        if strat != prev_strat:
            delta["strategy"] = strat

        # AI model monitor — compare by version only (cheap; payload can be large).
        ai = full.get("ai")
        prev_ai = self._prev_snapshot.get("ai")
        ai_missing = (ai is None) != (prev_ai is None)
        if ai_missing or (ai or {}).get("version") != (prev_ai or {}).get("version"):
            delta["ai"] = ai

        # Account — include when changed
        acc = full.get("account")
        prev_acc = self._prev_snapshot.get("account")
        if acc != prev_acc:
            delta["account"] = acc

        # Market — only send when slug changes
        mkt = full.get("market")
        prev_mkt = self._prev_snapshot.get("market")
        if (mkt or {}).get("slug") != (prev_mkt or {}).get("slug"):
            delta["market"] = mkt
        elif mkt != prev_mkt:
            delta["market"] = mkt

        # Order books — include when changed
        ob = full.get("orderbooks")
        prev_ob = self._prev_snapshot.get("orderbooks")
        if ob != prev_ob:
            delta["orderbooks"] = ob

        # Open orders — include when changed
        oo = full.get("open_orders")
        prev_oo = self._prev_snapshot.get("open_orders")
        if oo != prev_oo:
            delta["open_orders"] = oo

        # History tails — only send every 2s
        if now - self._last_full_broadcast >= self._full_broadcast_interval:
            # Send only the latest 50 points of each history for merge recovery
            for hkey in ("btc_history", "sim_history"):
                hval = full.get(hkey, [])
                if hval:
                    delta[hkey] = hval[-50:]
            # Full equity history is small enough to always include
            eq = full.get("equity_history")
            if eq:
                delta["equity_history"] = eq
            # Price history — only latest 50 points
            ph = full.get("price_history", [])
            if ph:
                delta["price_history"] = ph[-50:]

        # Strategy trades — include when changed
        st = full.get("strategy_trades")
        prev_st = self._prev_snapshot.get("strategy_trades")
        if st != prev_st:
            delta["strategy_trades"] = st

        # Error state
        err = full.get("last_error")
        prev_err = self._prev_snapshot.get("last_error")
        if err != prev_err:
            delta["last_error"] = err

        # Candle sequence / active window
        for key in ("btc_candle_start_ts", "candle_seq"):
            cur = full.get(key)
            prev = self._prev_snapshot.get(key)
            if cur != prev:
                delta[key] = cur

        # Activity log (only latest entry if changed)
        act = full.get("activity", [])
        prev_act = self._prev_snapshot.get("activity", [])
        if act and (not prev_act or act[0].get("timestamp") != prev_act[0].get("timestamp")):
            delta["activity"] = act[:5]

        return delta

    async def run(self) -> None:
        while True:
            version = self.state.version
            if version != self._last_version and self.clients:
                self._last_version = version
                now = time.time()
                send_full = (now - self._last_full_broadcast >= self._full_broadcast_interval)

                # Serialising history is the main CPU cost; keep it off the loop.
                full = await asyncio.to_thread(
                    self.state.get_snapshot, 900 if send_full else 0
                )

                if send_full:
                    payload = full
                    self._last_full_broadcast = now
                else:
                    payload = self._build_delta(full)

                self._prev_snapshot = full

                if not payload or (len(payload) <= 2 and "t" in payload):
                    await asyncio.sleep(self.push_interval)
                    continue

                clients = list(self.clients)
                results = await asyncio.gather(
                    *(ws.send_json(payload) for ws in clients),
                    return_exceptions=True,
                )
                for ws, result in zip(clients, results):
                    if isinstance(result, Exception):
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
        try:
            init_strat = get_strategy(config.strategy_id, config.strategy_params())
            self.state.update(
                strategy={
                    "id": init_strat.meta.id,
                    "name": init_strat.meta.name,
                    "mode": config.trading_mode,
                    "params": dict(init_strat.params),
                    "equity": float(config.paper_initial_capital),
                    "initial": float(config.paper_initial_capital),
                    "peak": float(config.paper_initial_capital),
                    "return_pct": 0.0,
                    "pending": None,
                    "entered_this_candle": False,
                    "signal_side": "hold",
                    "signal_reason": "Standby",
                    "losses_streak": 0,
                    "wins_recent": 0,
                }
            )
        except Exception as err:
            logger.warning(f"Failed to populate initial strategy state: {err}")
        self.hub = BroadcastHub(self.state, config.dashboard_push_ms)
        self.feed = MarketFeed(config, self.state)
        self.btc_feed = BtcPriceFeed(self.state, asset=config.asset)
        self.wallet = WalletMonitor(config, self.state)
        self.health = HealthMonitor(config, self.state)
        self._bot: TradingBot | None = None
        self._thread: threading.Thread | None = None
        self._feed_task: asyncio.Task | None = None
        self._btc_feed_task: asyncio.Task | None = None
        self._hub_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._wallet_task: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start_bot(self) -> None:
        if self.is_running:
            # A stop may have been requested while the loop is finishing its
            # current (possibly slow) tick. Wait for it, then start fresh so the
            # Start button never silently no-ops.
            if not self.state.should_bot_stop():
                return
            thread = self._thread
            if thread is not None:
                thread.join(timeout=15.0)
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

    def reset_account(self, initial_capital: float = 100.0) -> None:
        if self._bot is not None:
            self._bot.reset_account(initial_capital)
        else:
            self.state.reset_account(initial_capital)
            clear_session(self.config, initial_capital)
            self.state.log(
                "info",
                f"Account balance reset to ${initial_capital:.2f} and all prior trading data deleted.",
            )

    def update_strategy(self, strategy_id: str, params: dict | None = None) -> dict:
        if self._bot is not None:
            strat = self._bot.update_strategy(strategy_id, params)
        else:
            strat = get_strategy(strategy_id, params)
            self.config.strategy_id = strategy_id
            self.config.custom_strategy_params = dict(strat.params)
            self.state.update(
                strategy={
                    "id": strat.meta.id,
                    "name": strat.meta.name,
                    "mode": self.config.trading_mode,
                    "params": dict(strat.params),
                    "equity": float(self.config.paper_initial_capital),
                    "initial": float(self.config.paper_initial_capital),
                    "peak": float(self.config.paper_initial_capital),
                    "return_pct": 0.0,
                    "pending": None,
                    "entered_this_candle": False,
                    "signal_side": "hold",
                    "signal_reason": "Standby",
                    "losses_streak": 0,
                    "wins_recent": 0,
                }
            )
            self.state.log("info", f"Strategy updated to {strat.meta.name} (params: {strat.params})")
        return {
            "ok": True,
            "strategy_id": strat.meta.id,
            "strategy_name": strat.meta.name,
            "params": dict(strat.params),
        }

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
            self.health.set_bot_alive(self.is_running)
            await asyncio.sleep(5)

    async def start_hub(self) -> None:
        if self._hub_task is None or self._hub_task.done():
            self._hub_task = asyncio.create_task(self.hub.run())

    async def ensure_wallet_monitor(self) -> None:
        if self._wallet_task is None or self._wallet_task.done():
            logger.info("Starting wallet monitor")
            self._wallet_task = asyncio.create_task(self.wallet.run())

    async def ensure_health_monitor(self) -> None:
        if self._health_task is None or self._health_task.done():
            self.health.set_bot_alive(self.is_running)
            self._health_task = asyncio.create_task(self.health.run())

    async def shutdown_background_tasks(self) -> None:
        """Cancel and join service tasks so Uvicorn can exit cleanly."""
        tasks = [
            task
            for task in (
                self._watchdog_task,
                self._feed_task,
                self._btc_feed_task,
                self._hub_task,
                self._wallet_task,
                self._health_task,
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
    app = FastAPI(title="Signull", version="0.3.0")

    if DASHBOARD_DIR.exists():
        app.mount("/static", StaticFiles(directory=DASHBOARD_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(
            DASHBOARD_DIR / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/api/status")
    async def status():
        snap = await asyncio.to_thread(service.state.get_snapshot, 900)
        snap["bot_thread_alive"] = service.is_running
        snap["stop_requested"] = service.state.should_bot_stop()
        return snap

    @app.get("/api/config")
    async def get_config():
        strat = (
            service._bot.strategy
            if service._bot is not None and getattr(service._bot, "strategy", None) is not None
            else None
        )
        return {
            "trading_mode": config.trading_mode,
            "asset": config.asset,
            "order_size_usdc": config.order_size_usdc,
            "max_entry_price": config.max_entry_price,
            "dashboard_push_ms": config.dashboard_push_ms,
            "bot_poll_interval_sec": config.bot_poll_interval_sec,
            "has_wallet": config.has_wallet,
            "strategy": config.strategy_id,
            "strategy_name": (
                strat.meta.name if strat is not None else config.strategy_id
            ),
            "paper_initial_capital": config.paper_initial_capital,
            "strategy_params": strat.params if strat is not None else config.strategy_params(),
            "gas_warn_bnb": config.gas_warn_bnb,
        }

    @app.post("/api/strategy/update")
    async def strategy_update(req: StrategyUpdateRequest):
        try:
            return await asyncio.to_thread(service.update_strategy, req.strategy_id, req.params)
        except KeyError as err:
            raise HTTPException(status_code=404, detail=str(err))
        except Exception as err:
            raise HTTPException(status_code=422, detail=str(err))

    @app.get("/api/ai/settings")
    async def ai_settings_get():
        return public_ai_settings()

    @app.get("/api/ai/models")
    async def ai_models_get():
        try:
            models = await asyncio.to_thread(list_models)
        except OpenRouterError as err:
            raise HTTPException(status_code=502, detail=str(err)) from err
        return {
            "models": models,
            "free_count": sum(1 for m in models if m.get("free")),
        }

    @app.post("/api/ai/settings")
    async def ai_settings_post(req: AISettingsRequest):
        api_key = "" if req.clear_key else req.api_key
        try:
            await asyncio.to_thread(
                save_ai_settings, api_key=api_key, model=req.model, base_url=req.base_url
            )
        except OSError as err:
            raise HTTPException(status_code=500, detail=f"Could not save settings: {err}") from err
        service.state.log("info", "AI settings updated (OpenRouter)")
        return public_ai_settings()

    @app.post("/api/ai/reset")
    async def ai_reset():
        bot = service._bot
        strat = getattr(bot, "strategy", None) if bot is not None else None
        reset_fn = getattr(strat, "reset_ai", None)
        if not callable(reset_fn):
            raise HTTPException(status_code=409, detail="AI strategy is not active")
        reset_fn()
        service.state.log("info", "AI conversation memory reset")
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        service.health.set_bot_alive(service.is_running)
        return await asyncio.to_thread(service.health.snapshot)

    @app.get("/api/wallet/verify")
    async def wallet_verify():
        check = await asyncio.to_thread(verify_wallet, config)
        service.state.merge_account({
            "connected": check.api_connected,
            "verified": check.ok,
            "signer_address": check.signer_address,
            "funder_address": check.funder_address,
            "signature_type": check.signature_type,
            "signature_label": check.signature_label,
            "balance_usdt": check.balance_usdt,
            "balance_usdc": check.balance_usdt,
            "gas_bnb": check.gas_bnb,
            "gas_pol": check.gas_bnb,
            "issues": check.issues,
            "tips": check.tips,
            "updated_at": time.time(),
            "mode": config.trading_mode,
        })
        return check.to_dict()

    @app.post("/api/orders/cancel")
    async def cancel_order(req: CancelOrderRequest):
        if not config.is_live:
            raise HTTPException(status_code=400, detail="Cancel is only available in live mode")
        client = None
        if service._bot is not None:
            client = service._bot.client
        elif service.wallet.client is not None:
            client = service.wallet.client
        if client is None or not client.is_authenticated:
            raise HTTPException(status_code=503, detail="Wallet is not authenticated")
        try:
            result = await asyncio.to_thread(client.cancel_order, req.order_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "result": result}

    @app.post("/api/bot/start")
    async def bot_start():
        await asyncio.to_thread(service.start_bot)
        return {"ok": True, "running": service.is_running}

    @app.post("/api/bot/stop")
    async def bot_stop():
        service.stop_bot()
        return {"ok": True, "running": service.is_running}

    @app.post("/api/account/reset")
    async def account_reset():
        if config.is_live:
            raise HTTPException(
                status_code=403,
                detail="Cannot reset a live wallet. Paper reset is disabled in TRADING_MODE=live.",
            )
        await asyncio.to_thread(service.reset_account, 100.0)
        return {"ok": True, "balance": 100.0, "message": "Account reset to $100"}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        service.hub.add(ws)
        try:
            # Send immediate full snapshot on connect
            snap = await asyncio.to_thread(service.state.get_snapshot, 900)
            await ws.send_json(snap)
            while True:
                try:
                    raw = await asyncio.wait_for(ws.receive_text(), timeout=60)
                except asyncio.TimeoutError:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "ping":
                    await ws.send_json({
                        "type": "pong",
                        "t": msg.get("t"),
                        "server_t": time.time(),
                    })
        except (WebSocketDisconnect, asyncio.CancelledError):
            # Normal disconnect or application shutdown. CancelledError is a
            # BaseException in modern Python, so it is not covered below.
            pass
        except Exception:
            pass
        finally:
            service.hub.remove(ws)

    # Live + backtest APIs on one origin so the unified dashboard can toggle instantly.
    register_backtest_routes(app)

    @app.on_event("startup")
    async def startup():
        service.state.clear_bot_stop()
        service.start_bot()
        await service.ensure_feed()
        await service.ensure_btc_feed()
        await service.ensure_wallet_monitor()
        await service.ensure_health_monitor()
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
