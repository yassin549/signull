"""
Download and cache Binance BTCUSDT 1-second klines for model inference.

Same public archive as the sim prototype:
  https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1s/
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

SIGNULL_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = SIGNULL_ROOT / "data" / "btc_1s" / "daily"
VISION_BASE = "https://data.binance.vision/data/spot/daily/klines/BTCUSDT/1s"

KLINE_COLUMNS = [
    "open_time_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time_ms",
    "quote_volume",
    "n_trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
]
NUMERIC_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "n_trades",
    "taker_buy_base",
    "taker_buy_quote",
]


def _daterange(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError(f"end {end} before start {start}")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _parse_day_zip(content: bytes, day: date) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if not names:
            raise ValueError(f"{day}: zip has no CSV")
        with zf.open(names[0]) as handle:
            raw = pd.read_csv(handle, header=None, names=KLINE_COLUMNS)

    if not pd.api.types.is_numeric_dtype(raw["open_time_ms"]):
        raw = raw[pd.to_numeric(raw["open_time_ms"], errors="coerce").notna()]
    raw["open_time_ms"] = pd.to_numeric(raw["open_time_ms"], errors="coerce")
    raw = raw.dropna(subset=["open_time_ms"]).copy()
    raw["open_time_ms"] = raw["open_time_ms"].astype("int64")

    sample = int(raw["open_time_ms"].iloc[0])
    if sample >= 10**15:
        raw["open_time_ms"] = raw["open_time_ms"] // 1000
        raw["close_time_ms"] = (
            pd.to_numeric(raw["close_time_ms"], errors="coerce").astype("int64") // 1000
        )
    elif sample < 10**12:
        raw["open_time_ms"] = raw["open_time_ms"] * 1000

    for col in NUMERIC_COLUMNS:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    raw = raw.drop(columns=["ignore"], errors="ignore")
    raw["timestamp"] = pd.to_datetime(raw["open_time_ms"], unit="ms", utc=True)

    day_start = pd.Timestamp(day, tz="UTC")
    day_end = day_start + pd.Timedelta(days=1)
    raw = raw[(raw["timestamp"] >= day_start) & (raw["timestamp"] < day_end)].copy()
    raw = (
        raw.sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="last")
        .reset_index(drop=True)
    )
    return raw


def _fill_missing_seconds(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    frame = frame.sort_values("timestamp").set_index("timestamp")
    full_index = pd.date_range(
        start=frame.index.min().floor("s"),
        end=frame.index.max().floor("s"),
        freq="1s",
        tz="UTC",
    )
    frame = frame.reindex(full_index)
    for col in ("open", "high", "low", "close"):
        frame[col] = frame[col].ffill().bfill()
    for col in (
        "volume",
        "quote_volume",
        "n_trades",
        "taker_buy_base",
        "taker_buy_quote",
    ):
        if col in frame.columns:
            frame[col] = frame[col].fillna(0.0)
    frame["open_time_ms"] = ((frame.index.asi8) // 10**6).astype("int64")
    # pandas datetime64[ns] → asi8 is ns; for ms index asi8 may already be ms
    sample = int(frame.index.asi8[0]) if len(frame) else 0
    if sample >= 10**17:
        frame["open_time_ms"] = (frame.index.asi8 // 10**6).astype("int64")
    elif sample >= 10**14:
        frame["open_time_ms"] = (frame.index.asi8 // 10**3).astype("int64")
    else:
        frame["open_time_ms"] = frame.index.asi8.astype("int64")
    frame["close_time_ms"] = frame["open_time_ms"] + 999
    frame = frame.reset_index(names="timestamp")
    return frame


def _day_cache_path(day: date) -> Path:
    return CACHE_DIR / f"BTCUSDT-1s-{day.isoformat()}.parquet"


def ensure_days(
    start: date,
    end: date,
    *,
    progress_callback=None,
) -> list[Path]:
    """
    Ensure daily 1s parquets exist for [start, end] inclusive.
    Downloads missing days from Binance Vision.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    days = _daterange(start, end)
    session = requests.Session()
    session.headers.update({"User-Agent": "signull-direction-model/1.0", "Accept": "*/*"})
    paths: list[Path] = []
    missing_files: list[str] = []

    for i, day in enumerate(days):
        path = _day_cache_path(day)
        if path.exists():
            paths.append(path)
            if progress_callback:
                progress_callback(
                    {
                        "type": "status",
                        "phase": "loading",
                        "message": f"BTC 1s cache hit {day.isoformat()} ({i + 1}/{len(days)})",
                        "candles_completed": i + 1,
                        "candles_total": len(days),
                    }
                )
            continue

        url = f"{VISION_BASE}/BTCUSDT-1s-{day.isoformat()}.zip"
        content = None
        for attempt in range(6):
            try:
                resp = session.get(url, timeout=(15, 120))
                if resp.status_code == 404:
                    content = None
                    break
                if resp.status_code == 429:
                    time.sleep(2**attempt)
                    continue
                resp.raise_for_status()
                content = resp.content
                break
            except requests.RequestException as exc:
                logger.warning("Download %s failed: %s", day, exc)
                time.sleep(2**attempt)
        if content is None:
            missing_files.append(day.isoformat())
            today = datetime.now(timezone.utc).date()
            if day == today:
                logger.info("Binance 1s daily archive for today (%s) is not yet published on Binance Vision.", day)
            else:
                logger.warning("No Binance 1s file for %s (404 or failed)", day)
            continue

        frame = _parse_day_zip(content, day)
        frame = _fill_missing_seconds(frame)
        frame.to_parquet(path, index=False, compression="zstd")
        paths.append(path)
        logger.info("Cached BTC 1s %s (%s rows)", day, len(frame))
        if progress_callback:
            progress_callback(
                {
                    "type": "status",
                    "phase": "loading",
                    "message": f"Downloaded BTC 1s {day.isoformat()} ({i + 1}/{len(days)})",
                    "candles_completed": i + 1,
                    "candles_total": len(days),
                }
            )
        time.sleep(0.15)

    if not paths:
        raise FileNotFoundError(
            f"No BTCUSDT 1s data available for {start} → {end}. "
            f"Missing/unpublished: {missing_files[:10]}"
        )
    if missing_files:
        today_iso = datetime.now(timezone.utc).date().isoformat()
        past_missing = [d for d in missing_files if d != today_iso]
        if past_missing:
            logger.warning("Missing %s past day file(s): %s…", len(past_missing), past_missing[:5])
    return paths


def load_1s_range(
    start_ts: int,
    end_ts: int,
    *,
    history_pad_seconds: int = 7200,
    progress_callback=None,
) -> pd.DataFrame:
    """
    Load continuous UTC 1s OHLCV covering [start_ts - pad, end_ts].

    Downloads any missing calendar days into data/btc_1s/daily/.
    """
    t0 = start_ts - int(history_pad_seconds)
    t1 = end_ts
    start_day = datetime.fromtimestamp(t0, tz=timezone.utc).date()
    end_day = datetime.fromtimestamp(max(t0, t1 - 1), tz=timezone.utc).date()
    # Also try loading from sim raw two-week parquet as a fast path seed
    _seed_from_sim_if_present(start_day, end_day)

    paths = ensure_days(start_day, end_day, progress_callback=progress_callback)
    frames = [pd.read_parquet(p) for p in paths]
    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = (
        df.sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="last")
        .reset_index(drop=True)
    )

    t_start = pd.Timestamp(t0, unit="s", tz="UTC")
    t_end = pd.Timestamp(t1, unit="s", tz="UTC")
    df = df[(df["timestamp"] >= t_start) & (df["timestamp"] < t_end)].copy()

    # Ensure contiguity inside the sliced window
    if df.empty:
        raise ValueError(f"No 1s rows in [{t_start}, {t_end})")
    df = _fill_missing_seconds(df)

    required = [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "n_trades",
        "taker_buy_base",
        "taker_buy_quote",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"1s frame missing columns: {missing}")
    return df.reset_index(drop=True)


def _seed_from_sim_if_present(start_day: date, end_day: date) -> None:
    """Copy days out of sim's combined parquet into the daily cache when present."""
    sim_parquet = (
        Path(__file__).resolve().parents[2].parent
        / "sim"
        / "data"
        / "raw"
        / "btc"
        / "btcusdt_1s_2w.parquet"
    )
    if not sim_parquet.exists():
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    need = [d for d in _daterange(start_day, end_day) if not _day_cache_path(d).exists()]
    if not need:
        return
    try:
        raw = pd.read_parquet(sim_parquet, columns=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not seed from sim parquet: %s", exc)
        return
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    for day in need:
        day_start = pd.Timestamp(day, tz="UTC")
        day_end = day_start + pd.Timedelta(days=1)
        part = raw[(raw["timestamp"] >= day_start) & (raw["timestamp"] < day_end)].copy()
        if part.empty:
            continue
        part = _fill_missing_seconds(part)
        part.to_parquet(_day_cache_path(day), index=False, compression="zstd")
        logger.info("Seeded 1s cache for %s from sim parquet", day)
