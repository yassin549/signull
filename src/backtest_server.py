"""FastAPI server for the backtesting dashboard."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.backtest.data import fetch_candles, prefetch_progress
from src.backtest.engine import run_backtest
from src.backtest.registry import get_strategy, list_strategies

logger = logging.getLogger(__name__)

BACKTEST_DIR = Path(__file__).resolve().parent.parent / "backtest_dashboard"


class BacktestRequest(BaseModel):
    strategy_id: str
    asset: str = "btc"
    candle_count: int = Field(default=100, ge=10, le=500)
    initial_capital: float = Field(default=100.0, gt=0)
    params: dict | None = None
    use_cache: bool = True


def create_backtest_app() -> FastAPI:
    app = FastAPI(title="Signull Backtest", version="0.1.0")

    if BACKTEST_DIR.exists():
        app.mount("/static", StaticFiles(directory=BACKTEST_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(BACKTEST_DIR / "index.html")

    @app.get("/api/strategies")
    async def strategies():
        return {"strategies": list_strategies()}

    @app.get("/api/cache")
    async def cache_status(asset: str = "btc", count: int = 100):
        return prefetch_progress(asset, count)

    @app.post("/api/run")
    async def run(req: BacktestRequest):
        try:
            strategy = get_strategy(req.strategy_id, req.params)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        logger.info(
            "Backtest %s — %s candles, $%.2f start",
            req.strategy_id,
            req.candle_count,
            req.initial_capital,
        )

        candles = fetch_candles(
            asset=req.asset,
            count=req.candle_count,
            use_cache=req.use_cache,
        )
        if not candles:
            raise HTTPException(status_code=404, detail="No resolved candles found for backtest")

        result = run_backtest(
            strategy,
            candles,
            initial_capital=req.initial_capital,
        )
        return result.to_dict()

    return app