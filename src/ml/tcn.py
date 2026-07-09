"""Temporal Convolutional Network for position sizing."""

from __future__ import annotations

import torch
import torch.nn as nn


class _TCNBlock(nn.Module):
    def __init__(self, in_ch: int, out_ch: int, kernel: int, dilation: int):
        super().__init__()
        pad = (kernel - 1) * dilation
        self.conv = nn.Conv1d(in_ch, out_ch, kernel, padding=pad, dilation=dilation)
        self.relu = nn.ReLU()
        self.drop = nn.Dropout(0.1)
        self.norm = nn.BatchNorm1d(out_ch)
        self.res = nn.Conv1d(in_ch, out_ch, 1) if in_ch != out_ch else nn.Identity()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.conv(x)
        out = out[:, :, : x.size(2)]
        out = self.norm(out)
        out = self.relu(out)
        out = self.drop(out)
        return out + self.res(x)


class TCNSizer(nn.Module):
    """
    Predict risk fraction in [min_risk, max_risk] from BTC sequence + equity state.

    Does NOT decide entry — sizing only.
    """

    def __init__(
        self,
        seq_len: int = 60,
        btc_channels: int = 3,
        equity_dim: int = 7,
        hidden: int = 32,
        min_risk: float = 0.05,
        max_risk: float = 0.50,
    ):
        super().__init__()
        self.min_risk = min_risk
        self.max_risk = max_risk

        blocks = []
        ch = btc_channels
        for dilation in (1, 2, 4, 8):
            blocks.append(_TCNBlock(ch, hidden, kernel=3, dilation=dilation))
            ch = hidden
        self.tcn = nn.Sequential(*blocks)

        self.equity_mlp = nn.Sequential(
            nn.Linear(equity_dim, 16),
            nn.ReLU(),
            nn.Linear(16, 16),
            nn.ReLU(),
        )

        self.head = nn.Sequential(
            nn.Linear(hidden + 16, 32),
            nn.ReLU(),
            nn.Dropout(0.1),
            nn.Linear(32, 1),
            nn.Sigmoid(),
        )

    def forward(self, btc_seq: torch.Tensor, equity_feat: torch.Tensor) -> torch.Tensor:
        """
        btc_seq: (B, seq_len, btc_channels) -> internally (B, C, T)
        equity_feat: (B, equity_dim)
        Returns: (B,) risk fractions
        """
        x = btc_seq.transpose(1, 2)
        x = self.tcn(x)
        x = x[:, :, -1]
        e = self.equity_mlp(equity_feat)
        h = torch.cat([x, e], dim=1)
        s = self.head(h).squeeze(-1)
        return self.min_risk + s * (self.max_risk - self.min_risk)