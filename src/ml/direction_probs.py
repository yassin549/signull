"""
Load per-second BTC 5m direction probabilities from the sim TCN prototype.

The research model lives in the sibling `sim/` repo. For fast backtests we use
precomputed (candle × 300) probability arrays keyed by candle open unix time.

Decision layer (this module) is intentionally independent of Polymarket quotes.
Fill price is still taken from the market tick when a trade is opened.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

SIGNULL_ROOT = Path(__file__).resolve().parents[2]
# Default: sibling checkout next to signull/
DEFAULT_SIM_ROOT = SIGNULL_ROOT.parent / "sim"
LOCAL_PROBS_PATH = SIGNULL_ROOT / "models" / "candle_direction" / "probs_by_candle.npz"

CANDLE_SECONDS = 300


def _resolve_sim_root(sim_root: Path | None = None) -> Path:
    if sim_root is not None:
        return Path(sim_root)
    import os

    env = os.environ.get("SIM_ROOT") or os.environ.get("BTC5M_SIM_ROOT")
    if env:
        return Path(env)
    return DEFAULT_SIM_ROOT


def _load_candle_unix_and_probs(sim_root: Path) -> tuple[np.ndarray, np.ndarray]:
    """
    Returns
    -------
    candle_unix : (n,) int64 open times in UTC seconds
    probs : (n, 300) float32 P(Up) per second
    """
    seq_path = sim_root / "data" / "processed" / "sequences.npz"
    # Prefer calibrated if present; fall back to raw TCN
    cal_path = sim_root / "models" / "tcn" / "predictions_calibrated.npz"
    raw_path = sim_root / "models" / "tcn" / "predictions_raw.npz"

    if not seq_path.exists():
        raise FileNotFoundError(
            f"Missing {seq_path}. Build the sim dataset first "
            f"(python scripts/build_dataset.py in the sim repo)."
        )

    pred_path = cal_path if cal_path.exists() else raw_path
    if not pred_path.exists():
        raise FileNotFoundError(
            f"Missing TCN predictions at {cal_path} or {raw_path}. "
            f"Run train_tcn.py / calibrate_model.py in the sim repo."
        )

    seq = np.load(seq_path, allow_pickle=False)
    if "candle_ids_unix" in seq.files:
        candle_unix = seq["candle_ids_unix"].astype(np.int64)
    elif "candle_ids_ms" in seq.files:
        candle_unix = (seq["candle_ids_ms"] // 1000).astype(np.int64)
    else:
        raw_ids = seq["candle_ids_ns"]
        sample = int(raw_ids[0]) if len(raw_ids) else 0
        if sample >= 10**17:
            candle_unix = (raw_ids // 10**9).astype(np.int64)
        elif sample >= 10**14:
            candle_unix = (raw_ids // 10**6).astype(np.int64)
        else:
            # stored as milliseconds despite legacy key name
            candle_unix = (raw_ids // 1000).astype(np.int64)

    probs = np.load(pred_path)["p"].astype(np.float32)
    if probs.shape[0] != len(candle_unix):
        raise ValueError(
            f"Prediction rows {probs.shape[0]} != candle rows {len(candle_unix)}"
        )
    if probs.shape[1] != CANDLE_SECONDS:
        raise ValueError(f"Expected {CANDLE_SECONDS} timesteps, got {probs.shape[1]}")

    source = "calibrated" if pred_path == cal_path else "raw"
    logger.info(
        "Loaded direction probs: n=%s source=%s range=%s->%s",
        len(candle_unix),
        source,
        int(candle_unix[0]),
        int(candle_unix[-1]),
    )
    return candle_unix, probs


def export_local_cache(sim_root: Path | None = None, out_path: Path | None = None) -> Path:
    """Write a self-contained probs cache under signull/models/."""
    sim_root = _resolve_sim_root(sim_root)
    out_path = out_path or LOCAL_PROBS_PATH
    candle_unix, probs = _load_candle_unix_and_probs(sim_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out_path,
        candle_unix=candle_unix,
        p=probs,
        sim_root=np.array(str(sim_root)),
    )
    return out_path


class DirectionProbStore:
    """In-memory map: candle_open_unix → P(Up) array of length 300."""

    def __init__(self) -> None:
        self._by_start: dict[int, np.ndarray] = {}
        self.n_loaded: int = 0
        self.source: str = ""
        self.first_ts: int | None = None
        self.last_ts: int | None = None

    @classmethod
    def from_sim(cls, sim_root: Path | None = None) -> DirectionProbStore:
        store = cls()
        root = _resolve_sim_root(sim_root)
        candle_unix, probs = _load_candle_unix_and_probs(root)
        store._ingest(candle_unix, probs)
        store.source = f"sim:{root}"
        return store

    @classmethod
    def from_local(cls, path: Path | None = None) -> DirectionProbStore:
        path = path or LOCAL_PROBS_PATH
        if not path.exists():
            raise FileNotFoundError(f"Local probs cache missing: {path}")
        data = np.load(path, allow_pickle=True)
        store = cls()
        store._ingest(data["candle_unix"].astype(np.int64), data["p"].astype(np.float32))
        store.source = f"local:{path}"
        return store

    @classmethod
    def load_auto(cls, sim_root: Path | None = None) -> DirectionProbStore:
        """
        Prefer the local signull cache (fast, self-contained), then the sim repo.

        Local cache is what `scripts/export_direction_probs.py` writes and is the
        reliable path for the dashboard server process.
        """
        errors: list[str] = []
        if LOCAL_PROBS_PATH.exists():
            try:
                return cls.from_local(LOCAL_PROBS_PATH)
            except Exception as exc:  # noqa: BLE001 — fall through to sim
                errors.append(f"local cache: {exc}")
        root = _resolve_sim_root(sim_root)
        try:
            return cls.from_sim(root)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"sim repo: {exc}")
        raise FileNotFoundError(
            "Could not load candle-direction probabilities. "
            + " | ".join(errors)
            + f" Run: py -3.12 scripts/export_direction_probs.py "
            f"(expected cache at {LOCAL_PROBS_PATH})"
        )

    def _ingest(self, candle_unix: np.ndarray, probs: np.ndarray) -> None:
        self._by_start = {
            int(ts): probs[i]
            for i, ts in enumerate(candle_unix.tolist())
        }
        self.n_loaded = len(self._by_start)
        if self.n_loaded:
            keys = list(self._by_start.keys())
            self.first_ts = min(keys)
            self.last_ts = max(keys)
        else:
            self.first_ts = self.last_ts = None

    def time_range_iso(self) -> tuple[str, str]:
        """Human-readable UTC range of available model probs."""
        from datetime import datetime, timezone

        def fmt(ts: int | None) -> str:
            if ts is None:
                return "?"
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )

        return fmt(self.first_ts), fmt(self.last_ts)

    def has_candle(self, start_ts: int) -> bool:
        return int(start_ts) in self._by_start

    def prob_up_series(self, start_ts: int) -> np.ndarray | None:
        return self._by_start.get(int(start_ts))

    def prob_up_at(self, start_ts: int, seconds_elapsed: int) -> float | None:
        series = self.prob_up_series(start_ts)
        if series is None:
            return None
        s = int(seconds_elapsed)
        if s < 0 or s >= CANDLE_SECONDS:
            return None
        return float(series[s])

    def first_threshold_cross(
        self,
        start_ts: int,
        threshold: float,
        *,
        up_to_elapsed: int,
    ) -> tuple[str, int, float] | None:
        """
        First second in [0, up_to_elapsed] where model confidence hits threshold.

        Returns (side, elapsed_second, model_prob_for_side) or None.
        side is 'up' if P(Up) >= threshold, 'down' if P(Down) >= threshold.
        If both hit on the same second, the higher confidence side wins.
        """
        series = self.prob_up_series(start_ts)
        if series is None:
            return None
        thr = float(threshold)
        last = min(int(up_to_elapsed), CANDLE_SECONDS - 1)
        if last < 0:
            return None
        for s in range(0, last + 1):
            p_up = float(series[s])
            p_down = 1.0 - p_up
            up_hit = p_up >= thr
            down_hit = p_down >= thr
            if not up_hit and not down_hit:
                continue
            if up_hit and down_hit:
                if p_up >= p_down:
                    return ("up", s, p_up)
                return ("down", s, p_down)
            if up_hit:
                return ("up", s, p_up)
            return ("down", s, p_down)
        return None

    def coverage(self, start_timestamps: list[int]) -> dict[str, Any]:
        n = len(start_timestamps)
        hit = sum(1 for t in start_timestamps if self.has_candle(int(t)))
        return {
            "candles_requested": n,
            "candles_with_probs": hit,
            "coverage": (hit / n) if n else 0.0,
            "store_size": self.n_loaded,
            "source": self.source,
        }
