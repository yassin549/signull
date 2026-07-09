"""Load trained TCN and predict position size."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import torch

from src.ml.btc_features import fetch_klines, window_features
from src.ml.tcn import TCNSizer

DEFAULT_MODEL_PATH = Path(__file__).resolve().parent.parent.parent / "models" / "tcn_sizer.pt"


class TCNSizerPredictor:
    def __init__(self, model_path: Path | None = None):
        path = model_path or DEFAULT_MODEL_PATH
        if not path.exists():
            raise FileNotFoundError(f"TCN model not found: {path}. Run: python main.py train-sizer")

        ckpt = torch.load(path, map_location="cpu", weights_only=False)
        self.lookback = int(ckpt.get("lookback", 60))
        equity_dim = int(ckpt.get("equity_dim", 6))
        self.model = TCNSizer(
            seq_len=self.lookback,
            equity_dim=equity_dim,
            min_risk=float(ckpt.get("min_risk", 0.05)),
            max_risk=float(ckpt.get("max_risk", 0.50)),
        )
        self.model.load_state_dict(ckpt["model_state"])
        self.model.eval()

        self._klines_cache: dict[tuple[int, int], list] = {}
        self._feat_cache: dict[int, np.ndarray] = {}
        self._bulk_klines: list | None = None

    def preload(self, start_ts: int, end_ts: int) -> None:
        """Fetch BTC klines once for an entire backtest window."""
        self._bulk_klines = fetch_klines(start_ts, end_ts)
        self._feat_cache.clear()

    def _get_klines(self, entry_ts: int) -> list:
        start = entry_ts - self.lookback * 60 - 120
        end = entry_ts + 60
        if self._bulk_klines is not None:
            return self._bulk_klines
        key = (start, end)
        if key not in self._klines_cache:
            self._klines_cache[key] = fetch_klines(start, end)
        return self._klines_cache[key]

    def btc_features(self, entry_ts: int) -> np.ndarray | None:
        if entry_ts in self._feat_cache:
            return self._feat_cache[entry_ts]
        klines = self._get_klines(entry_ts)
        feats = window_features(klines, entry_ts, lookback=self.lookback)
        if feats is not None:
            self._feat_cache[entry_ts] = feats
        return feats

    def _with_side_momentum(
        self,
        equity_feats: np.ndarray,
        btc: np.ndarray,
        side: str,
    ) -> np.ndarray:
        ret = float(np.sum(btc[:, 0]))
        signed = ret if side == "up" else -ret
        eq = equity_feats.astype(np.float32).copy()
        expected = self.model.equity_mlp[0].in_features
        if eq.shape[0] < expected:
            eq = np.pad(eq, (0, expected - eq.shape[0]))
        elif eq.shape[0] > expected:
            eq = eq[:expected]
        if expected >= 7:
            eq[6] = float(np.tanh(signed * 40.0))
        return eq

    def predict(
        self,
        entry_ts: int,
        equity_feats: np.ndarray,
        *,
        side: str = "up",
    ) -> float:
        btc = self.btc_features(entry_ts)
        if btc is None:
            return self.model.min_risk

        eq = self._with_side_momentum(equity_feats, btc, side)

        with torch.no_grad():
            btc_t = torch.from_numpy(btc).unsqueeze(0)
            eq_t = torch.from_numpy(eq).unsqueeze(0)
            risk = self.model(btc_t, eq_t).item()
        return float(risk)