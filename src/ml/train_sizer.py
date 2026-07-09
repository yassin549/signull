"""Train TCN position sizer on historical data."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset, random_split

from src.ml.dataset import LOOKBACK, load_training_data
from src.ml.tcn import TCNSizer

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "models"
DEFAULT_MODEL_PATH = MODEL_DIR / "tcn_sizer.pt"


class SizerDataset(Dataset):
    def __init__(self, samples):
        self.samples = samples

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        s = self.samples[idx]
        return (
            torch.from_numpy(s.btc_feats),
            torch.from_numpy(s.equity_feats),
            torch.tensor(s.label_risk, dtype=torch.float32),
        )


def train_tcn_sizer(
    *,
    asset: str = "btc",
    candle_count: int = 500,
    epochs: int = 40,
    batch_size: int = 32,
    lr: float = 1e-3,
    save_path: Path = DEFAULT_MODEL_PATH,
) -> dict:
    samples = load_training_data(asset, candle_count)
    if len(samples) < 20:
        raise RuntimeError(f"Not enough samples ({len(samples)}). Fetch more candles.")

    dataset = SizerDataset(samples)
    n_val = max(1, int(len(dataset) * 0.15))
    n_train = len(dataset) - n_val
    train_ds, val_ds = random_split(
        dataset,
        [n_train, n_val],
        generator=torch.Generator().manual_seed(42),
    )

    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_ds, batch_size=batch_size)

    from src.ml.dataset import EQUITY_DIM

    model = TCNSizer(seq_len=LOOKBACK, equity_dim=EQUITY_DIM)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    loss_fn = nn.MSELoss()

    best_val = float("inf")
    patience = 8
    stale = 0
    history: list[dict] = []

    for epoch in range(1, epochs + 1):
        model.train()
        train_loss = 0.0
        for btc, eq, y in train_loader:
            pred = model(btc, eq)
            loss = loss_fn(pred, y)
            opt.zero_grad()
            loss.backward()
            opt.step()
            train_loss += loss.item() * len(y)

        model.eval()
        val_loss = 0.0
        with torch.no_grad():
            for btc, eq, y in val_loader:
                pred = model(btc, eq)
                val_loss += loss_fn(pred, y).item() * len(y)

        train_loss /= max(1, n_train)
        val_loss /= max(1, n_val)
        history.append({"epoch": epoch, "train": train_loss, "val": val_loss})

        if val_loss < best_val:
            best_val = val_loss
            stale = 0
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            torch.save({
                "model_state": model.state_dict(),
                "lookback": LOOKBACK,
                "equity_dim": 7,
                "min_risk": model.min_risk,
                "max_risk": model.max_risk,
                "samples": len(samples),
                "candles": candle_count,
                "asset": asset,
            }, save_path)
        else:
            stale += 1
            if stale >= patience:
                logger.info("Early stop at epoch %s (best val %.5f)", epoch, best_val)
                break

        if epoch % 5 == 0 or epoch == 1:
            logger.info("Epoch %s — train %.5f  val %.5f", epoch, train_loss, val_loss)

    meta = {
        "samples": len(samples),
        "candles": candle_count,
        "asset": asset,
        "best_val_loss": best_val,
        "model_path": str(save_path),
        "history": history[-5:],
    }
    (MODEL_DIR / "tcn_sizer_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta