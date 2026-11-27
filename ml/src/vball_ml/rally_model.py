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
    def __init__(
        self,
        n_in: int,
        width: int = 64,
        dilations: Sequence[int] = (1, 2, 4, 8, 16),
        in_dropout: float = 0.2,
    ):
        super().__init__()
        self.width = width
        self.inp = nn.Sequential(nn.Dropout(in_dropout), nn.Linear(n_in, width), nn.GELU())
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


def video_norm(x: F32) -> F32:
    """Standardise a video's features by its own statistics. Removes per-venue offsets
    (lighting, colour, camera, audio level); measured to be what makes the model carry over to
    gyms it hasn't seen (cross-gym F1 0.00/0.14 -> 0.59/0.73 on the first two matches)."""
    return ((x - x.mean(0)) / (x.std(0) + 1e-6)).astype(np.float32)


def add_motion(x: F32, video_dims: int = 384) -> F32:
    """Append |change| of the frame embedding between consecutive steps (5-step smoothed):
    how much the scene is moving. Measured cross-gym gain: LOMO F1 0.66 -> 0.78."""
    v = x[:, :video_dims]
    d = np.vstack([np.zeros((1, video_dims), np.float32), np.abs(np.diff(v, axis=0))])
    k = np.ones(5) / 5
    d = np.apply_along_axis(lambda c: np.convolve(c, k, mode="same"), 0, d)
    return np.hstack([x, d.astype(np.float32)])


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
    width: int = 64,
    in_dropout: float = 0.2,
    noise: float = 0.0,
) -> tuple[TemporalNet, Normaliser]:
    """Fit on the steps where ``mask`` is True (other steps are held out)."""
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    norm = Normaliser.fit(x[mask])
    xn = torch.from_numpy(norm(x))
    yt = torch.from_numpy(y)
    mt = torch.from_numpy(mask.astype(np.float32))
    net = TemporalNet(x.shape[1], width=width, in_dropout=in_dropout)
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
        batch = torch.stack(xb)
        if noise > 0:  # feature noise: robustness to venue/camera differences
            batch = batch + noise * torch.randn_like(batch)
        logits = net(batch)
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
    prob: F32,
    threshold: float = 0.5,
    min_gap_s: float = 1.0,
    min_len_s: float = 2.0,
    exit_threshold: float | None = None,
) -> list[tuple[float, float]]:
    """Probabilities -> rally spans (seconds). Generic, not fitted per video.

    With ``exit_threshold`` (hysteresis) a rally starts when the smoothed probability reaches
    ``threshold`` but only ends when it drops below ``exit_threshold``, so a brief dip in the
    middle of a long rally doesn't split it. Gaps shorter than ``min_gap_s`` are joined.
    """
    k = 2
    smooth = np.convolve(prob, np.ones(2 * k + 1) / (2 * k + 1), mode="same")
    lo = threshold if exit_threshold is None else exit_threshold
    spans: list[list[float]] = []
    i, n = 0, len(smooth)
    while i < n:
        if smooth[i] >= threshold:
            j = i
            while j < n and smooth[j] >= lo:
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


@dataclass
class Bundle:
    """A trained rally model: an ensemble of nets (averaged) + the input normaliser."""

    nets: list[TemporalNet]
    norm: Normaliser
    meta: dict[str, object]

    def prepare(self, x: F32) -> F32:
        """Raw extracted features -> model input, exactly as the model was trained."""
        if self.meta.get("motion"):
            x = add_motion(x)
        if self.meta.get("input_norm") == "per_video":
            x = video_norm(x)
        return x

    def predict(self, x: F32) -> F32:
        """P(in rally) per step, from raw extracted features."""
        x = self.prepare(x)
        return np.mean([predict(n, self.norm, x) for n in self.nets], axis=0).astype(np.float32)

    def decode_params(self) -> dict[str, float]:
        d = self.meta.get("decode")
        return dict(d) if isinstance(d, dict) else {}

    def rallies(self, x: F32) -> tuple[F32, list[tuple[float, float]]]:
        prob = self.predict(x)
        return prob, decode(prob, **self.decode_params())


def save_bundle(path: str, bundle: Bundle) -> None:
    torch.save(
        {
            "format": 1,
            "n_in": int(bundle.norm.mean.shape[0]),
            "mean": bundle.norm.mean,
            "std": bundle.norm.std,
            "states": [n.state_dict() for n in bundle.nets],
            "width": bundle.nets[0].width,
            "meta": bundle.meta,
        },
        path,
    )


def load_bundle(path: str) -> Bundle:
    blob = torch.load(path, map_location="cpu", weights_only=False)
    if blob.get("format") != 1:
        raise ValueError("unsupported rally model format")
    nets = []
    for state in blob["states"]:
        net = TemporalNet(blob["n_in"], width=int(blob.get("width", 64)))
        net.load_state_dict(state)
        net.eval()
        nets.append(net)
    return Bundle(nets, Normaliser(blob["mean"], blob["std"]), dict(blob["meta"]))
