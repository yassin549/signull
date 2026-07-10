"""FastAPI server for the backtesting dashboard."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.backtest.data import fetch_candles, prefetch_progress
from src.backtest.engine import run_backtest
from src.backtest.registry import get_strategy, list_strategies
from src.config import SERIES_SLUGS

BACKTEST_DIR = Path(__file__).resolve().parent.parent / "backtest_dashboard"


class BacktestRequest(BaseModel):
    strategy_id: str
    asset: str = "btc"
    candle_count: int = Field(default=100, ge=10, le=500)
    initial_capital: float = Field(default=100.0, gt=0)
    params: dict | None = None
    use_cache: bool = True


def _run_backtest(req: BacktestRequest) -> dict:
    strategy = get_strategy(req.strategy_id, req.params)
    candles = fetch_candles(asset=req.asset, count=req.candle_count, use_cache=req.use_cache)
    if not candles:
        raise LookupError("No resolved candles found for backtest")
    return run_backtest(strategy, candles, initial_capital=req.initial_capital).to_dict()


def create_backtest_app() -> FastAPI:
    app = FastAPI(title="Signull Backtest", version="0.1.0")
    run_slots = asyncio.Semaphore(1)

    if BACKTEST_DIR.exists():
        app.mount("/static", StaticFiles(directory=BACKTEST_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(BACKTEST_DIR / "index.html")

    @app.get("/api/strategies")
    async def strategies():
        return {"strategies": await asyncio.to_thread(list_strategies)}

    @app.get("/api/cache")
    async def cache_status(asset: str = "btc", count: int = 100):
        if asset not in SERIES_SLUGS:
            raise HTTPException(status_code=422, detail="Unsupported asset")
        return await asyncio.to_thread(prefetch_progress, asset, count)

    @app.post("/api/run")
    async def run(req: BacktestRequest):
        if req.asset not in SERIES_SLUGS:
            raise HTTPException(status_code=422, detail="Unsupported asset")
        if run_slots.locked():
            raise HTTPException(status_code=429, detail="A backtest is already running; try again shortly")
        try:
            async with run_slots:
                return await asyncio.to_thread(_run_backtest, req)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app
