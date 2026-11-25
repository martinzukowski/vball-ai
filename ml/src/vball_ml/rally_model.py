"""Learned rally detector: a small temporal convolutional network over frozen DINOv2 frame
embeddings + audio band energies, predicting P(in rally) for every 0.2 s step. Rallies are
then read off with a fixed, generic decoding (smooth -> threshold -> join short gaps -> drop
blips); nothing in it is fitted to a particular video.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
import torch
from torch import nn

STEP = 0.2
F32 = npt.NDArray[np.float32]


class TemporalNet(nn.Module):
    def __init__(self, n_in: int, width: int = 64, dilations: Sequence[int] = (1, 2, 4, 8, 16)):
        super().__init__()
        self.inp = nn.Sequential(nn.Dropout(0.2), nn.Linear(n_in, width), nn.GELU())
        self.blocks = nn.ModuleList(
            nn.Sequential(
                nn.Conv1d(width, width, 5, padding=2 * d, dilation=d),
                nn.GELU(),
                nn.Dropout(0.2),
                nn.Conv1d(width, width, 1),
            )
            for d in dilations
        )
        self.out = nn.Conv1d(width, 1, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # x: [B, T, F] -> logits [B, T]
        h = self.inp(x).transpose(1, 2)
        for block in self.blocks:
            h = h + block(h)
        return self.out(h).squeeze(1)


def labels_for(n_steps: int, rallies: Sequence[tuple[float, float]]) -> F32:
    y = np.zeros(n_steps, np.float32)
    for s, e in rallies:
        y[round(s / STEP) : round(e / STEP) + 1] = 1.0
    return y


@dataclass
class Normaliser:
    mean: F32
    std: F32

    @classmethod
    def fit(cls, x: F32) -> Normaliser:
        return cls(x.mean(0), x.std(0) + 1e-6)

    def __call__(self, x: F32) -> F32:
        return ((x - self.mean) / self.std).astype(np.float32)


def train(
    x: F32,
    y: F32,
    mask: npt.NDArray[np.bool_],
    *,
    epochs: int = 250,
    crop: int = 600,
    seed: int = 0,
) -> tuple[TemporalNet, Normaliser]:
    """Fit on the steps where ``mask`` is True (other steps are held out)."""
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    norm = Normaliser.fit(x[mask])
    xn = torch.from_numpy(norm(x))
    yt = torch.from_numpy(y)
    mt = torch.from_numpy(mask.astype(np.float32))
    net = TemporalNet(x.shape[1])
    opt = torch.optim.AdamW(net.parameters(), lr=1e-3, weight_decay=1e-2)
    pos = float(y[mask].mean())
    loss_fn = nn.BCEWithLogitsLoss(
        pos_weight=torch.tensor((1 - pos) / max(pos, 1e-3)), reduction="none"
    )
    train_idx = np.flatnonzero(mask)
    net.train()
    for _ in range(epochs):
        starts = rng.choice(train_idx, size=8)
        xb, yb, mb = [], [], []
        for s in starts:
            s = int(min(s, len(x) - crop))
            xb.append(xn[s : s + crop])
            yb.append(yt[s : s + crop])
            mb.append(mt[s : s + crop])
        logits = net(torch.stack(xb))
        loss = (loss_fn(logits, torch.stack(yb)) * torch.stack(mb)).sum() / torch.stack(mb).sum()
        opt.zero_grad()
        loss.backward()
        opt.step()
    net.eval()
    return net, norm


def predict(net: TemporalNet, norm: Normaliser, x: F32) -> F32:
    with torch.inference_mode():
        return torch.sigmoid(net(torch.from_numpy(norm(x))[None]))[0].numpy()


def decode(
    prob: F32, threshold: float = 0.5, min_gap_s: float = 1.0, min_len_s: float = 2.0
) -> list[tuple[float, float]]:
    """Probabilities -> rally spans (seconds). Generic, not fitted per video."""
    k = 2
    smooth = np.convolve(prob, np.ones(2 * k + 1) / (2 * k + 1), mode="same")
    on = smooth >= threshold
    spans: list[list[float]] = []
    i, n = 0, len(on)
    while i < n:
        if on[i]:
            j = i
            while j < n and on[j]:
                j += 1
            s, e = i * STEP, j * STEP
            if spans and s - spans[-1][1] < min_gap_s:
                spans[-1][1] = e
            else:
                spans.append([s, e])
            i = j
        else:
            i += 1
    return [(round(s, 1), round(e, 1)) for s, e in spans if e - s >= min_len_s]
