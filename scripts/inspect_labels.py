"""Inspect TCN training labels and model predictions (no network calls)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ml.dataset import load_training_data
from src.ml.predictor import TCNSizerPredictor


def _predict_from_sample(predictor: TCNSizerPredictor, sample) -> float:
    eq = sample.equity_feats.astype(np.float32).copy()
    expected = predictor.model.equity_mlp[0].in_features
    if eq.shape[0] < expected:
        eq = np.pad(eq, (0, expected - eq.shape[0]))
    elif eq.shape[0] > expected:
        eq = eq[:expected]

    with torch.no_grad():
        btc_t = torch.from_numpy(sample.btc_feats).unsqueeze(0)
        eq_t = torch.from_numpy(eq).unsqueeze(0)
        return float(predictor.model(btc_t, eq_t).item())


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    samples = load_training_data("btc", count)
    if not samples:
        print("No samples.")
        sys.exit(1)

    labels = [s.label_risk for s in samples]
    wins = [s for s in samples if s.won]
    losses = [s for s in samples if not s.won]

    print(f"Samples: {len(samples)}")
    print(f"Labels: min={min(labels):.3f} max={max(labels):.3f} mean={np.mean(labels):.3f}")
    print(f"  wins mean={np.mean([s.label_risk for s in wins]):.3f}")
    print(f"  losses mean={np.mean([s.label_risk for s in losses]):.3f}")
    print(f"  pct>35%={sum(1 for v in labels if v > 0.35) / len(labels):.1%}")
    print(f"  pct<10%={sum(1 for v in labels if v < 0.10) / len(labels):.1%}")

    predictor = TCNSizerPredictor()
    preds = [_predict_from_sample(predictor, s) for s in samples]

    print(f"Preds: min={min(preds):.3f} max={max(preds):.3f} mean={np.mean(preds):.3f}")
    print(f"  on win samples mean={np.mean([preds[i] for i, s in enumerate(samples) if s.won]):.3f}")
    print(f"  on loss samples mean={np.mean([preds[i] for i, s in enumerate(samples) if not s.won]):.3f}")

    err = np.mean([abs(p - s.label_risk) for p, s in zip(preds, samples)])
    print(f"Mean abs error vs teacher: {err:.4f}")


if __name__ == "__main__":
    main()