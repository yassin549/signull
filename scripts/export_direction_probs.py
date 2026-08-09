"""Export sim TCN per-candle probabilities into signull/models for backtests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ml.direction_probs import LOCAL_PROBS_PATH, export_local_cache  # noqa: E402


def main() -> None:
    path = export_local_cache()
    import numpy as np

    data = np.load(path)
    unix = data["candle_unix"]
    print(f"Exported {len(unix)} candles -> {path}")
    print(f"Range UTC unix: {int(unix[0])} -> {int(unix[-1])}")
    print(f"Shape p: {data['p'].shape}")
    print(f"Also available at default: {LOCAL_PROBS_PATH}")


if __name__ == "__main__":
    main()
