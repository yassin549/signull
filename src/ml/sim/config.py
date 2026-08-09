"""Central configuration for sim 1.1 model integration in Signull."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SIGNULL_ROOT = Path(__file__).resolve().parents[2]
LOCAL_MODEL_DIR = SIGNULL_ROOT / "models" / "candle_direction"

CANDLE_SECONDS = 300
CANDLE_FREQ = "5min"

# Numerical stability
EPS = 1e-8
VOL_EPS = 1e-6

# Rolling windows (seconds)
MOMENTUM_WINDOWS = (5, 10, 20, 30, 60)
VOL_WINDOWS = (10, 20, 30, 60)
VOLUME_WINDOWS = (10, 30, 60)
PREV_CANDLE_CONTEXT = 12  # completed 5m candles (~1 hour)

MIN_VALID_ELAPSED = 0  # keep all 300; long windows use pre-candle history


@dataclass
class TCNConfig:
    """Hyperparameters for the causal Temporal Convolutional Network."""

    n_features: int = 0  # set at runtime
    hidden_channels: list[int] = field(default_factory=lambda: [64, 64, 64, 64])
    kernel_size: int = 3
    dropout: float = 0.1
    learning_rate: float = 1e-3
    weight_decay: float = 1e-5
    batch_size: int = 64
    max_epochs: int = 40
    patience: int = 8
    seed: int = 42


TCN_DEFAULT = TCNConfig()
