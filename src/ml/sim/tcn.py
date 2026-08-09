"""Causal Temporal Convolutional Network for per-second Up probability."""

from __future__ import annotations

from typing import Sequence

import torch
import torch.nn as nn
from torch.nn.utils.parametrizations import weight_norm

from .config import TCNConfig


class Chomp1d(nn.Module):
    """Remove trailing padding to keep convolutions causal."""

    def __init__(self, chomp_size: int) -> None:
        super().__init__()
        self.chomp_size = chomp_size

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.chomp_size == 0:
            return x
        return x[:, :, : -self.chomp_size].contiguous()


class TemporalBlock(nn.Module):
    def __init__(
        self,
        n_inputs: int,
        n_outputs: int,
        kernel_size: int,
        dilation: int,
        dropout: float,
    ) -> None:
        super().__init__()
        padding = (kernel_size - 1) * dilation
        self.conv1 = weight_norm(
            nn.Conv1d(
                n_inputs,
                n_outputs,
                kernel_size,
                padding=padding,
                dilation=dilation,
            )
        )
        self.chomp1 = Chomp1d(padding)
        self.relu1 = nn.ReLU()
        self.drop1 = nn.Dropout(dropout)

        self.conv2 = weight_norm(
            nn.Conv1d(
                n_outputs,
                n_outputs,
                kernel_size,
                padding=padding,
                dilation=dilation,
            )
        )
        self.chomp2 = Chomp1d(padding)
        self.relu2 = nn.ReLU()
        self.drop2 = nn.Dropout(dropout)

        self.downsample = (
            nn.Conv1d(n_inputs, n_outputs, 1) if n_inputs != n_outputs else None
        )
        self.relu = nn.ReLU()
        self.init_weights()

    def init_weights(self) -> None:
        self.conv1.weight.data.normal_(0, 0.01)
        self.conv2.weight.data.normal_(0, 0.01)
        if self.downsample is not None:
            self.downsample.weight.data.normal_(0, 0.01)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.conv1(x)
        out = self.chomp1(out)
        out = self.relu1(out)
        out = self.drop1(out)

        out = self.conv2(out)
        out = self.chomp2(out)
        out = self.relu2(out)
        out = self.drop2(out)

        res = x if self.downsample is None else self.downsample(x)
        return self.relu(out + res)


class CausalTCN(nn.Module):
    """
    Causal TCN mapping (batch, T, F) -> (batch, T) Up probabilities via sigmoid.
    """

    def __init__(self, config: TCNConfig) -> None:
        super().__init__()
        if config.n_features <= 0:
            raise ValueError("TCNConfig.n_features must be set > 0")

        layers: list[nn.Module] = []
        channels: list[int] = [config.n_features] + list(config.hidden_channels)
        for i in range(len(config.hidden_channels)):
            dilation = 2**i
            layers.append(
                TemporalBlock(
                    channels[i],
                    channels[i + 1],
                    kernel_size=config.kernel_size,
                    dilation=dilation,
                    dropout=config.dropout,
                )
            )
        self.network = nn.Sequential(*layers)
        self.head = nn.Conv1d(channels[-1], 1, kernel_size=1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Parameters
        ----------
        x : Tensor, shape (batch, T, F)

        Returns
        -------
        probs : Tensor, shape (batch, T) in (0, 1)
        """
        h = x.transpose(1, 2)
        h = self.network(h)
        logits = self.head(h).squeeze(1)  # (B, T)
        return torch.sigmoid(logits)

    def forward_logits(self, x: torch.Tensor) -> torch.Tensor:
        h = x.transpose(1, 2)
        h = self.network(h)
        return self.head(h).squeeze(1)


def build_tcn(
    n_features: int,
    hidden_channels: Sequence[int] | None = None,
    kernel_size: int = 3,
    dropout: float = 0.1,
) -> CausalTCN:
    cfg = TCNConfig(
        n_features=n_features,
        hidden_channels=list(hidden_channels) if hidden_channels is not None else [64, 64, 64, 64],
        kernel_size=kernel_size,
        dropout=dropout,
    )
    return CausalTCN(cfg)
