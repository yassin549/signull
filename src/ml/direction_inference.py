"""
Online inference for the sim BTC 5m direction TCN.

This is the real model path:
  BTC 1s bars → causal features → scaler → TCN → P(Up) per second

Precomputed caches are only a speed optimization for the original two-week set.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Callable

import joblib
import numpy as np
import pandas as pd
import torch

from src.ml.btc_1s import load_1s_range
from src.ml.direction_probs import (
    CANDLE_SECONDS,
    DEFAULT_SIM_ROOT,
    DirectionProbStore,
    LOCAL_PROBS_PATH,
    _resolve_sim_root,
)

logger = logging.getLogger(__name__)

SIGNULL_ROOT = Path(__file__).resolve().parents[2]
LOCAL_MODEL_DIR = SIGNULL_ROOT / "models" / "candle_direction"


def _ensure_sim_on_path(sim_root: Path) -> None:
    src = str(sim_root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def _artifact_paths(sim_root: Path | None = None) -> dict[str, Path]:
    """Resolve model artifacts from signull local dir or sim repo."""
    local_ckpt = LOCAL_MODEL_DIR / "best.pt"
    local_scaler = LOCAL_MODEL_DIR / "scaler.joblib"
    local_features = LOCAL_MODEL_DIR / "feature_names.json"
    local_cal = LOCAL_MODEL_DIR / "calibrator.joblib"

    sim_root = _resolve_sim_root(sim_root) if sim_root else DEFAULT_SIM_ROOT
    sim_ckpt = sim_root / "models" / "tcn" / "best.pt"
    sim_scaler = sim_root / "data" / "processed" / "scaler.joblib"
    sim_features = sim_root / "data" / "processed" / "feature_names.json"
    sim_cal = sim_root / "models" / "tcn" / "calibrator.joblib"

    ckpt = local_ckpt if local_ckpt.exists() else sim_ckpt
    scaler = local_scaler if local_scaler.exists() else sim_scaler
    features = local_features if local_features.exists() else sim_features
    cal = local_cal if local_cal.exists() else sim_cal

    if not ckpt.exists():
        raise FileNotFoundError(
            f"TCN checkpoint not found at {local_ckpt} or {sim_ckpt}."
        )
    if not scaler.exists():
        raise FileNotFoundError(
            f"Scaler not found at {local_scaler} or {sim_scaler}."
        )
    if not features.exists():
        raise FileNotFoundError(f"feature_names.json missing at {features}")

    return {
        "checkpoint": ckpt,
        "scaler": scaler,
        "feature_names": features,
        "calibrator": cal if cal.exists() else None,  # type: ignore[dict-item]
    }


def sync_artifacts_to_local(sim_root: Path | None = None) -> None:
    """Copy sim model artifacts into signull/models/candle_direction/."""
    import json
    import shutil

    root = _resolve_sim_root(sim_root)
    paths = _artifact_paths(root)
    LOCAL_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for key in ("checkpoint", "scaler", "feature_names"):
        src = paths[key]
        dst = LOCAL_MODEL_DIR / (
            "best.pt"
            if key == "checkpoint"
            else "scaler.joblib"
            if key == "scaler"
            else "feature_names.json"
        )
        if src.resolve() != dst.resolve() and src.exists():
            shutil.copy2(src, dst)
    cal = paths.get("calibrator")
    if cal and Path(cal).exists():
        shutil.copy2(cal, LOCAL_MODEL_DIR / "calibrator.joblib")
    meta = {
        "synced_from": str(root),
        "checkpoint": str(paths["checkpoint"]),
    }
    (LOCAL_MODEL_DIR / "artifact_meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


class DirectionModel:
    """Loaded TCN + scaler ready for batch inference on feature tensors."""

    def __init__(self, sim_root: Path | None = None, device: str | None = None):
        self.sim_root = _resolve_sim_root(sim_root) if sim_root else None
        paths = _artifact_paths(self.sim_root)

        from src.ml.sim.config import TCNConfig
        from src.ml.sim.tcn import CausalTCN

        self.device = torch.device(
            device or ("cuda" if torch.cuda.is_available() else "cpu")
        )
        ckpt = torch.load(paths["checkpoint"], map_location=self.device, weights_only=False)
        cfg = TCNConfig(**ckpt["config"])
        self.model = CausalTCN(cfg).to(self.device)
        self.model.load_state_dict(ckpt["model_state"])
        self.model.eval()

        self.scaler = joblib.load(paths["scaler"])
        import json

        self.feature_names = json.loads(
            Path(paths["feature_names"]).read_text(encoding="utf-8")
        )
        self.calibrator = None
        cal_path = paths.get("calibrator")
        if cal_path:
            try:
                obj = joblib.load(cal_path)
                self.calibrator = obj.get("calibrator", obj)
                logger.info("Loaded calibrator from %s", cal_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not load calibrator: %s", exc)

        self.n_features = len(self.feature_names)
        logger.info(
            "DirectionModel ready device=%s features=%s ckpt=%s",
            self.device,
            self.n_features,
            paths["checkpoint"],
        )

    @torch.no_grad()
    def predict_sequences(self, X: np.ndarray, batch_size: int = 64) -> np.ndarray:
        """
        X: (n, 300, F) unscaled features → (n, 300) P(Up)
        """
        from src.ml.sim.dataset import transform_X

        if X.ndim != 3:
            raise ValueError(f"Expected X rank 3, got {X.shape}")
        if X.shape[2] != self.n_features:
            raise ValueError(
                f"Feature dim {X.shape[2]} != model {self.n_features}"
            )
        Xs = transform_X(X, self.scaler).astype(np.float32)
        outs: list[np.ndarray] = []
        for start in range(0, len(Xs), batch_size):
            xb = torch.from_numpy(Xs[start : start + batch_size]).to(self.device)
            pb = self.model(xb).cpu().numpy()
            outs.append(pb)
        p = np.concatenate(outs, axis=0).astype(np.float64)
        if self.calibrator is not None:
            try:
                p = np.asarray(self.calibrator.transform(p), dtype=np.float64)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Calibrator transform failed: %s", exc)
        return np.clip(p, 0.0, 1.0)


def infer_probs_for_range(
    start_ts: int,
    end_ts: int,
    *,
    sim_root: Path | None = None,
    history_pad_seconds: int = 7200,
    progress_callback: Callable[[dict], None] | None = None,
    model: DirectionModel | None = None,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """
    Run full causal pipeline on BTC 1s data covering [start_ts, end_ts).

    Returns
    -------
    candle_unix : (n,) open times
    probs : (n, 300) P(Up)
    meta : diagnostics
    """
    if progress_callback:
        progress_callback(
            {
                "type": "status",
                "phase": "loading",
                "message": "Loading BTC 1-second market data for model inference…",
            }
        )

    bars_1s = load_1s_range(
        start_ts,
        end_ts,
        history_pad_seconds=history_pad_seconds,
        progress_callback=progress_callback,
    )
    logger.info(
        "1s bars for inference: %s rows %s → %s",
        len(bars_1s),
        bars_1s["timestamp"].iloc[0],
        bars_1s["timestamp"].iloc[-1],
    )

    if progress_callback:
        progress_callback(
            {
                "type": "status",
                "phase": "loading",
                "message": "Building causal features + running TCN…",
            }
        )

    from src.ml.sim.candles import build_candle_table
    from src.ml.sim.features import build_feature_frame, frames_to_sequences

    second_bars, meta = build_candle_table(bars_1s)
    featured, feature_cols = build_feature_frame(second_bars, meta)
    X, y, elapsed, remaining, candle_ids = frames_to_sequences(featured, feature_cols)

    model = model or DirectionModel(sim_root)
    # Feature order must match training
    if list(feature_cols) != list(model.feature_names):
        # Reorder if same set
        if set(feature_cols) != set(model.feature_names):
            raise ValueError(
                "Feature mismatch vs trained model. "
                f"pipeline={len(feature_cols)} model={len(model.feature_names)}"
            )
        idx = [feature_cols.index(n) for n in model.feature_names]
        X = X[:, :, idx]

    probs = model.predict_sequences(X)
    candle_unix = np.array(
        [int(pd.Timestamp(ts).timestamp()) for ts in candle_ids],
        dtype=np.int64,
    )

    # Keep only candles whose open falls inside the requested window
    mask = (candle_unix >= int(start_ts)) & (candle_unix < int(end_ts))
    candle_unix = candle_unix[mask]
    probs = probs[mask]

    info = {
        "n_1s_bars": int(len(bars_1s)),
        "n_candles_scored": int(len(candle_unix)),
        "n_features": int(X.shape[2]),
        "source": "online_inference",
        "model_device": str(model.device),
        "first_ts": int(candle_unix[0]) if len(candle_unix) else None,
        "last_ts": int(candle_unix[-1]) if len(candle_unix) else None,
    }
    logger.info(
        "Inferred probs for %s candles (%s → %s)",
        info["n_candles_scored"],
        info["first_ts"],
        info["last_ts"],
    )
    return candle_unix, probs.astype(np.float32), info


def build_store_for_candles(
    candles,
    *,
    sim_root: Path | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    use_precomputed_when_available: bool = True,
) -> DirectionProbStore:
    """
    Build a DirectionProbStore covering every backtest candle via inference.

    Optionally merges precomputed two-week cache first (fast path), then runs
    online inference for the full requested span so *new* dates are scored too.
    """
    if not candles:
        raise ValueError("No candles for direction model")

    starts = sorted(int(c.start_ts) for c in candles)
    t0 = starts[0]
    t1 = max(int(c.end_ts) for c in candles)

    store = DirectionProbStore()
    # Always run online inference for the requested window so any historical
    # range with public Binance 1s data can be scored — not only the train set.
    if progress_callback:
        progress_callback(
            {
                "type": "status",
                "phase": "loading",
                "message": (
                    f"Running direction TCN on {len(starts)} candles "
                    f"(downloading BTC 1s if needed)…"
                ),
            }
        )

    # Seed from precomputed for speed on overlapping days (optional)
    precomputed_n = 0
    if use_precomputed_when_available:
        try:
            pre = DirectionProbStore.load_auto(sim_root)
            # Only keep keys we might need — full merge is fine
            store._by_start.update(pre._by_start)
            precomputed_n = pre.n_loaded
            store.source = f"precomputed+inference({pre.source})"
        except Exception as exc:  # noqa: BLE001
            logger.info("No precomputed probs to seed (%s); inference only", exc)
            store.source = "online_inference"

    candle_unix, probs, info = infer_probs_for_range(
        t0,
        t1,
        sim_root=sim_root,
        progress_callback=progress_callback,
    )
    # Inference results overwrite precomputed for the same keys (same model path)
    for i, ts in enumerate(candle_unix.tolist()):
        store._by_start[int(ts)] = probs[i]
    store.n_loaded = len(store._by_start)
    if store.n_loaded:
        keys = list(store._by_start.keys())
        store.first_ts = min(keys)
        store.last_ts = max(keys)
    store.source = (
        f"online_inference n_scored={info['n_candles_scored']} "
        f"precomputed_seed={precomputed_n}"
    )
    info["precomputed_seed"] = precomputed_n
    store._last_infer_meta = info  # type: ignore[attr-defined]
    return store
