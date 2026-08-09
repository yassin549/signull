"""Causal feature engineering for every second of each 5-minute candle."""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd
from scipy.special import erf

from .config import (
    CANDLE_SECONDS,
    EPS,
    MOMENTUM_WINDOWS,
    VOL_EPS,
    VOL_WINDOWS,
    VOLUME_WINDOWS,
)

FEATURE_COLUMNS: list[str] = []


def _log_return(series: pd.Series) -> pd.Series:
    return np.log(series / series.shift(1)).replace([np.inf, -np.inf], np.nan)


def add_series_level_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    px = out["close"].astype("float64")

    out["log_ret_1s"] = _log_return(px).fillna(0.0)

    for w in MOMENTUM_WINDOWS:
        out[f"log_ret_{w}s"] = np.log(px / px.shift(w)).replace([np.inf, -np.inf], np.nan)
        out[f"vel_{w}s"] = out[f"log_ret_{w}s"] / w

    out["accel_5_20"] = out["vel_5s"] - out["vel_20s"]
    out["accel_10_60"] = out["vel_10s"] - out["vel_60s"]

    for w in VOL_WINDOWS:
        out[f"rvol_{w}s"] = (
            out["log_ret_1s"].rolling(window=w, min_periods=max(2, w // 2)).std()
        )

    out["sell_volume"] = (out["volume"] - out["taker_buy_base"]).clip(lower=0.0)
    out["buy_volume"] = out["taker_buy_base"].astype("float64")
    out["trade_count"] = out["n_trades"].astype("float64")
    out["quote_volume"] = out["quote_volume"].astype("float64")

    total_vol = out["volume"].astype("float64")
    out["buy_sell_imbalance"] = (out["buy_volume"] - out["sell_volume"]) / (total_vol + EPS)
    out["taker_buy_ratio"] = out["buy_volume"] / (total_vol + EPS)
    out["avg_trade_size"] = total_vol / (out["trade_count"] + EPS)

    for w in VOLUME_WINDOWS:
        out[f"volume_sum_{w}s"] = total_vol.rolling(w, min_periods=1).sum()
        out[f"trade_count_sum_{w}s"] = out["trade_count"].rolling(w, min_periods=1).sum()
        out[f"buy_volume_sum_{w}s"] = out["buy_volume"].rolling(w, min_periods=1).sum()
        out[f"sell_volume_sum_{w}s"] = out["sell_volume"].rolling(w, min_periods=1).sum()
        out[f"imbalance_sum_{w}s"] = (
            (out[f"buy_volume_sum_{w}s"] - out[f"sell_volume_sum_{w}s"])
            / (out[f"volume_sum_{w}s"] + EPS)
        )

    trail_mean_vol = total_vol.rolling(3600, min_periods=60).mean()
    out["rel_volume_1s"] = total_vol / (trail_mean_vol + EPS)
    out["rel_volume_60s"] = out["volume_sum_60s"] / (60.0 * (trail_mean_vol + EPS))

    return out


def add_previous_candle_context(second_bars: pd.DataFrame, meta: pd.DataFrame) -> pd.DataFrame:
    m = meta.sort_values("candle_id").reset_index(drop=True).copy()

    m["prev_return"] = m["candle_log_return"].shift(1)
    m["prev_direction"] = m["label"].shift(1)
    m["prev_range"] = m["candle_range"].shift(1)
    m["prev_range_pct"] = (m["candle_range"] / m["candle_open"]).shift(1)

    m["prev_abs_return"] = m["candle_log_return"].abs().shift(1)
    m["prev_range_vol"] = m["prev_range_pct"]

    for n in (3, 6, 12):
        m[f"roll_return_{n}c"] = m["candle_log_return"].shift(1).rolling(n, min_periods=1).mean()
        m[f"roll_vol_{n}c"] = m["candle_log_return"].shift(1).rolling(n, min_periods=2).std()
        m[f"roll_abs_return_{n}c"] = (
            m["candle_log_return"].abs().shift(1).rolling(n, min_periods=1).mean()
        )

    directions = m["label"].to_numpy()
    streak = np.zeros(len(m), dtype=np.float64)
    for i in range(len(m)):
        if i == 0:
            streak[i] = np.nan
            continue
        run = 1.0
        sign = 1.0 if directions[i - 1] == 1 else -1.0
        j = i - 2
        while j >= 0 and directions[j] == directions[i - 1]:
            run += 1.0
            j -= 1
        streak[i] = sign * run
    m["prev_streak"] = streak

    context_cols = [
        c
        for c in m.columns
        if c.startswith("prev_") or c.startswith("roll_")
    ]
    ctx = m[["candle_id"] + context_cols].copy()

    merged = second_bars.merge(ctx, on="candle_id", how="left", validate="many_to_one")
    return merged


def add_candle_state_features(second_bars: pd.DataFrame) -> pd.DataFrame:
    out = second_bars.sort_values(["candle_id", "seconds_elapsed"]).copy()

    out["current_high"] = out.groupby("candle_id", sort=False)["high"].cummax()
    out["current_low"] = out.groupby("candle_id", sort=False)["low"].cummin()

    open_px = out["candle_open"].astype("float64")
    cur = out["close"].astype("float64")

    out["seconds_elapsed_f"] = out["seconds_elapsed"].astype("float64")
    out["seconds_remaining_f"] = out["seconds_remaining"].astype("float64")
    out["frac_elapsed"] = out["seconds_elapsed_f"] / float(CANDLE_SECONDS)
    out["frac_remaining"] = out["seconds_remaining_f"] / float(CANDLE_SECONDS)

    out["delta_open"] = cur - open_px
    out["log_ret_open"] = np.log(cur / open_px).replace([np.inf, -np.inf], np.nan)
    out["abs_delta_open"] = out["delta_open"].abs()
    out["dist_to_high"] = out["current_high"] - cur
    out["dist_to_low"] = cur - out["current_low"]
    out["candle_range_so_far"] = out["current_high"] - out["current_low"]
    out["range_position"] = (cur - out["current_low"]) / (out["candle_range_so_far"] + EPS)
    out["above_open"] = (cur > open_px).astype("float64")

    vol_scale = out["rvol_60s"].copy()
    vol_scale = vol_scale.fillna(out["rvol_30s"]).fillna(out["rvol_10s"]).fillna(VOL_EPS)
    vol_scale = vol_scale.clip(lower=VOL_EPS)
    out["vol_scale"] = vol_scale
    out["delta_open_over_vol"] = out["log_ret_open"] / vol_scale
    rem_seconds = out["seconds_remaining_f"].clip(lower=0.0) + 1.0
    out["delta_open_over_vol_sqrt_rem"] = out["log_ret_open"] / (
        vol_scale * np.sqrt(rem_seconds)
    )

    # Dynamic Z-Score and theoretical Brownian Motion Probability (60s local vol)
    rem_std_60s = vol_scale * np.sqrt(rem_seconds)
    z_score_60s = (out["log_ret_open"] / rem_std_60s).clip(-10.0, 10.0)
    out["z_score_60s"] = z_score_60s
    out["p_brownian_60s"] = (0.5 * (1.0 + erf(z_score_60s / np.sqrt(2.0)))).clip(1e-5, 1.0 - 1e-5)

    # Dynamic Z-Score and Brownian Motion Probability (multi-candle regime vol)
    roll_vol_col = out["roll_vol_6c"] if "roll_vol_6c" in out.columns else out.get("prev_range_vol", vol_scale)
    per_sec_vol_6c = (roll_vol_col / np.sqrt(300.0)).fillna(vol_scale).clip(lower=VOL_EPS)
    rem_std_candle = per_sec_vol_6c * np.sqrt(rem_seconds)
    z_score_candle = (out["log_ret_open"] / rem_std_candle).clip(-10.0, 10.0)
    out["z_score_candle"] = z_score_candle
    out["p_brownian_candle"] = (0.5 * (1.0 + erf(z_score_candle / np.sqrt(2.0)))).clip(1e-5, 1.0 - 1e-5)

    # Volatility-normalized velocities and acceleration
    for w in (5, 10, 20, 30, 60):
        if f"vel_{w}s" in out.columns:
            out[f"norm_vel_{w}s"] = out[f"vel_{w}s"] / (vol_scale + EPS)
    out["norm_accel_5_20"] = out["accel_5_20"] / (vol_scale + EPS)
    out["norm_accel_10_60"] = out["accel_10_60"] / (vol_scale + EPS)
    out["norm_delta_open"] = out["log_ret_open"] / (vol_scale * np.sqrt(300.0) + EPS)
    out["norm_range_so_far"] = (out["candle_range_so_far"] / open_px) / (vol_scale * np.sqrt(300.0) + EPS)

    out["cum_volume"] = out.groupby("candle_id", sort=False)["volume"].cumsum()
    out["cum_trades"] = out.groupby("candle_id", sort=False)["n_trades"].cumsum()
    out["cum_buy_volume"] = out.groupby("candle_id", sort=False)["buy_volume"].cumsum()
    out["cum_sell_volume"] = out.groupby("candle_id", sort=False)["sell_volume"].cumsum()
    out["cum_imbalance"] = (out["cum_buy_volume"] - out["cum_sell_volume"]) / (
        out["cum_volume"] + EPS
    )

    return out


def select_model_features(df: pd.DataFrame) -> list[str]:
    features = [
        "frac_elapsed",
        "frac_remaining",
        "seconds_elapsed_f",
        "seconds_remaining_f",
        "z_score_60s",
        "p_brownian_60s",
        "z_score_candle",
        "p_brownian_candle",
        "norm_vel_5s",
        "norm_vel_10s",
        "norm_vel_20s",
        "norm_vel_30s",
        "norm_vel_60s",
        "norm_accel_5_20",
        "norm_accel_10_60",
        "norm_delta_open",
        "norm_range_so_far",
        "delta_open",
        "log_ret_open",
        "abs_delta_open",
        "dist_to_high",
        "dist_to_low",
        "candle_range_so_far",
        "range_position",
        "above_open",
        "log_ret_1s",
        "log_ret_5s",
        "log_ret_10s",
        "log_ret_20s",
        "log_ret_30s",
        "log_ret_60s",
        "vel_5s",
        "vel_10s",
        "vel_20s",
        "vel_30s",
        "vel_60s",
        "accel_5_20",
        "accel_10_60",
        "rvol_10s",
        "rvol_20s",
        "rvol_30s",
        "rvol_60s",
        "vol_scale",
        "delta_open_over_vol",
        "delta_open_over_vol_sqrt_rem",
        "trade_count",
        "volume",
        "buy_volume",
        "sell_volume",
        "buy_sell_imbalance",
        "taker_buy_ratio",
        "avg_trade_size",
        "rel_volume_1s",
        "rel_volume_60s",
        "volume_sum_10s",
        "volume_sum_30s",
        "volume_sum_60s",
        "trade_count_sum_10s",
        "trade_count_sum_30s",
        "trade_count_sum_60s",
        "imbalance_sum_10s",
        "imbalance_sum_30s",
        "imbalance_sum_60s",
        "cum_volume",
        "cum_trades",
        "cum_imbalance",
        "prev_return",
        "prev_direction",
        "prev_range_pct",
        "prev_abs_return",
        "prev_range_vol",
        "prev_streak",
        "roll_return_3c",
        "roll_return_6c",
        "roll_return_12c",
        "roll_vol_3c",
        "roll_vol_6c",
        "roll_vol_12c",
        "roll_abs_return_3c",
        "roll_abs_return_6c",
        "roll_abs_return_12c",
    ]

    missing = [c for c in features if c not in df.columns]
    if missing:
        raise ValueError(f"Missing engineered features: {missing}")
    return features


def fill_feature_nans(df: pd.DataFrame, feature_cols: Sequence[str]) -> pd.DataFrame:
    for col in feature_cols:
        if col.startswith("rvol_") or col in ("vol_scale",) or ("vol" in col and col.startswith("roll_")):
            val = VOL_EPS
        else:
            val = 0.0
        df[col] = df[col].fillna(val).replace([np.inf, -np.inf], val)
    return df


def build_feature_frame(second_bars: pd.DataFrame, meta: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    bars = second_bars.sort_values("timestamp").reset_index(drop=True)
    bars = add_series_level_features(bars)
    bars = add_previous_candle_context(bars, meta)
    bars = add_candle_state_features(bars)
    feature_cols = select_model_features(bars)
    bars = fill_feature_nans(bars, feature_cols)

    global FEATURE_COLUMNS
    FEATURE_COLUMNS = list(feature_cols)
    return bars, feature_cols


def frames_to_sequences(
    featured: pd.DataFrame,
    feature_cols: Sequence[str],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, pd.DatetimeIndex]:
    featured = featured.sort_values(["candle_id", "seconds_elapsed"])
    candle_ids = featured["candle_id"].unique()
    n_candles = len(candle_ids)
    n_features = len(feature_cols)

    counts = featured.groupby("candle_id", sort=True).size()
    if not (counts == CANDLE_SECONDS).all():
        raise ValueError("All candles must have exactly 300 rows before sequencing")

    feat_mat = featured.loc[:, list(feature_cols)].to_numpy(dtype=np.float64)
    labels = featured.groupby("candle_id", sort=True)["label"].first().to_numpy(dtype=np.int64)
    elapsed = featured["seconds_elapsed"].to_numpy(dtype=np.int32).reshape(n_candles, CANDLE_SECONDS)
    remaining = featured["seconds_remaining"].to_numpy(dtype=np.int32).reshape(n_candles, CANDLE_SECONDS)
    X = feat_mat.reshape(n_candles, CANDLE_SECONDS, n_features)
    y = labels

    if not np.isfinite(X).all():
        raise ValueError("Non-finite values in feature tensor after fill")

    return X, y, elapsed, remaining, pd.DatetimeIndex(candle_ids)
