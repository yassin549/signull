"""Normalizer utilities for sim 1.0."""

from __future__ import annotations

import numpy as np
from sklearn.preprocessing import StandardScaler


def transform_X(X: np.ndarray, scaler: StandardScaler) -> np.ndarray:
    """Apply a fitted scaler; preserve (n, 300, F) shape."""
    if X.ndim != 3:
        raise ValueError(f"Expected 3D X, got shape {X.shape}")
    n, t, f = X.shape
    if hasattr(scaler, "n_features_in_") and scaler.n_features_in_ != f:
        raise ValueError(
            f"Scaler expects {scaler.n_features_in_} features, got {f}"
        )
    flat = X.reshape(n * t, f)
    scaled = scaler.transform(flat)
    return scaled.reshape(n, t, f).astype(np.float64)
