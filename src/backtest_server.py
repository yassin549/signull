"""FastAPI server for the backtesting dashboard."""

from __future__ import annotations

import asyncio
import json
import queue
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.backtest.data import fetch_candles, first_available_start, prefetch_progress
from src.backtest.engine import run_backtest
from src.backtest.registry import get_strategy, list_strategies
from src.config import SERIES_SLUGS

BACKTEST_DIR = Path(__file__).resolve().parent.parent / "backtest_dashboard"


class BacktestRequest(BaseModel):
    strategy_id: str
    asset: str = "btc"
    candle_count: int = Field(default=100, ge=10)
    # UTC Unix seconds.  These preserve the hour and minute selected in the UI.
    start_ts: int | None = None
    end_ts: int | None = None
    # Kept for compatibility with older dashboard pages.
    start_date: date | None = None
    end_date: date | None = None
    all_history: bool = False
    initial_capital: float = Field(default=100.0, gt=0)
    params: dict | None = None
    use_cache: bool = True


def _run_backtest(
    req: BacktestRequest, progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    params = dict(req.params or {})
    params.setdefault("asset", req.asset)
    strategy = get_strategy(req.strategy_id, params)
    has_precise_range = req.start_ts is not None or req.end_ts is not None
    has_date_range = req.start_date is not None or req.end_date is not None
    if has_precise_range and has_date_range:
        raise ValueError("Send either timestamps or dates, not both")
    if has_precise_range:
        if req.start_ts is None or req.end_ts is None:
            raise ValueError("Choose both a start and end time")
        start_ts, end_ts = req.start_ts, req.end_ts
        if end_ts <= start_ts:
            raise ValueError("End time must be after the start time")
    elif has_date_range:
        if req.start_date is None or req.end_date is None:
            raise ValueError("Choose both a start and end date")
        if req.end_date < req.start_date:
            raise ValueError("End date must be on or after the start date")
        start_ts = int(datetime.combine(req.start_date, time.min, tzinfo=timezone.utc).timestamp())
        end_ts = int(datetime.combine(req.end_date + timedelta(days=1), time.min, tzinfo=timezone.utc).timestamp())
    else:
        start_ts = end_ts = None
    candles = fetch_candles(
        asset=req.asset, count=req.candle_count, start_ts=start_ts, end_ts=end_ts,
        all_history=req.all_history, use_cache=req.use_cache,
        progress_callback=progress_callback,
    )
    if not candles:
        raise LookupError("No resolved candles found for backtest")
    result = run_backtest(
        strategy, candles, initial_capital=req.initial_capital,
        progress_callback=progress_callback,
    ).to_dict()
    # A requested period can contain gaps when a historical event is unavailable,
    # so return the exact resolved window that the engine received.
    result["data_start_ts"] = candles[0].start_ts
    result["data_end_ts"] = candles[-1].end_ts
    if start_ts is not None and end_ts is not None:
        first = ((start_ts + 299) // 300) * 300
        last = ((end_ts - 300) // 300) * 300
        requested = 0 if first > last else ((last - first) // 300) + 1
        result["candles_requested"] = requested
        result["candles_missing"] = max(0, requested - len(candles))
    return result


def create_backtest_app() -> FastAPI:
    app = FastAPI(title="Signull Backtest", version="0.1.0")
    run_slots = asyncio.Semaphore(1)
    run_active = False
    jobs: dict[str, queue.Queue[dict]] = {}

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

    @app.get("/api/availability")
    async def availability(asset: str = "btc"):
        if asset not in SERIES_SLUGS:
            raise HTTPException(status_code=422, detail="Unsupported asset")
        first = await asyncio.to_thread(first_available_start, asset)
        return {"asset": asset, "first_available_start_ts": first}

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
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/run/start")
    async def start_run(req: BacktestRequest):
        """Start a run and expose its data-load and simulation events via SSE."""
        nonlocal run_active
        if req.asset not in SERIES_SLUGS:
            raise HTTPException(status_code=422, detail="Unsupported asset")
        if run_active:
            raise HTTPException(status_code=429, detail="A backtest is already running; try again shortly")
        run_active = True
        job_id = uuid.uuid4().hex
        events: queue.Queue[dict] = queue.Queue()
        jobs[job_id] = events

        def emit(event: dict) -> None:
            events.put(event)

        async def worker() -> None:
            nonlocal run_active
            try:
                emit({"type": "status", "phase": "loading", "message": "Loading resolved market data"})
                async with run_slots:
                    result = await asyncio.to_thread(_run_backtest, req, emit)
                emit({"type": "complete", "result": result})
            except (KeyError, LookupError, ValueError) as exc:
                emit({"type": "error", "message": str(exc)})
            except Exception:
                emit({"type": "error", "message": "Backtest failed; check the server log."})
            finally:
                run_active = False

        asyncio.create_task(worker())
        return {"job_id": job_id}

    @app.get("/api/run/{job_id}/events")
    async def run_events(job_id: str):
        events = jobs.get(job_id)
        if events is None:
            raise HTTPException(status_code=404, detail="Unknown or expired backtest")

        async def stream():
            try:
                while True:
                    event = await asyncio.to_thread(events.get)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") in {"complete", "error"}:
                        break
            finally:
                jobs.pop(job_id, None)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
