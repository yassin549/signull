"""Exchange-aligned 5-minute candle reconstruction from 1-second bars."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import CANDLE_FREQ, CANDLE_SECONDS


def assign_candle_id(timestamps: pd.Series) -> pd.Series:
    """Floor each UTC timestamp to the exchange-aligned 5-minute boundary."""
    ts = pd.to_datetime(timestamps, utc=True)
    return ts.dt.floor(CANDLE_FREQ)


def build_candle_table(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Attach candle metadata to every 1s row and return (second_bars, candle_meta).
    """
    if "timestamp" not in df.columns:
        raise ValueError("df must contain timestamp")

    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    out = out.sort_values("timestamp").reset_index(drop=True)

    out["candle_id"] = assign_candle_id(out["timestamp"])
    out["seconds_elapsed"] = (
        (out["timestamp"] - out["candle_id"]).dt.total_seconds().astype("int64")
    )
    out["seconds_remaining"] = CANDLE_SECONDS - out["seconds_elapsed"]

    if (out["seconds_elapsed"] < 0).any() or (out["seconds_elapsed"] >= CANDLE_SECONDS).any():
        raise ValueError("seconds_elapsed out of [0, 299] — check candle alignment")

    grouped = out.groupby("candle_id", sort=True)
    counts = grouped.size()
    complete_ids = counts[counts == CANDLE_SECONDS].index

    out = out[out["candle_id"].isin(complete_ids)].copy()

    elapsed_ok = (
        out.groupby("candle_id")["seconds_elapsed"]
        .apply(lambda s: s.sort_values().to_numpy().tolist() == list(range(CANDLE_SECONDS)))
    )
    if not bool(elapsed_ok.all()):
        bad = elapsed_ok[~elapsed_ok].index.tolist()[:5]
        raise ValueError(f"Complete-count candles missing second grid, e.g. {bad}")

    candle_open = grouped["open"].first()
    candle_close = grouped["close"].last()
    candle_high = grouped["high"].max()
    candle_low = grouped["low"].min()
    candle_vol = grouped["volume"].sum()
    candle_buy_vol = grouped["taker_buy_base"].sum()
    candle_trades = grouped["n_trades"].sum()

    log_ret = np.log(candle_close / candle_open).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    label = (candle_close > candle_open).astype("int64")

    meta = pd.DataFrame(
        {
            "candle_id": complete_ids,
            "candle_open": candle_open.reindex(complete_ids).to_numpy(),
            "candle_high": candle_high.reindex(complete_ids).to_numpy(),
            "candle_low": candle_low.reindex(complete_ids).to_numpy(),
            "candle_close": candle_close.reindex(complete_ids).to_numpy(),
            "candle_volume": candle_vol.reindex(complete_ids).to_numpy(),
            "candle_buy_volume": candle_buy_vol.reindex(complete_ids).to_numpy(),
            "candle_trades": candle_trades.reindex(complete_ids).to_numpy(),
            "candle_log_return": log_ret.reindex(complete_ids).to_numpy(),
            "candle_range": (
                candle_high.reindex(complete_ids) - candle_low.reindex(complete_ids)
            ).to_numpy(),
            "label": label.reindex(complete_ids).to_numpy(),
        }
    ).reset_index(drop=True)

    c_map = meta.set_index("candle_id")
    out["candle_open"] = out["candle_id"].map(c_map["candle_open"]).astype("float64")
    out["candle_close"] = out["candle_id"].map(c_map["candle_close"]).astype("float64")
    out["label"] = out["candle_id"].map(c_map["label"]).astype("int64")

    return out, meta
