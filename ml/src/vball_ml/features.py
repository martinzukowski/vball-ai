"""Learned-model input features, one row per 0.2 s step:

* video: DINOv2-small embedding (CLS token, 384-d) of the frame cropped to the play area's
  bounding box (whole frame if none). Frozen, pretrained (Apache-2.0); nothing volleyball-
  specific is hand-coded.
* audio: 32 log-spaced frequency bands (log energy) over the same 0.2 s window.

Cached as ``.npz`` keyed by the video file's size + mtime + extractor version.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import numpy.typing as npt
import torch
from transformers import AutoImageProcessor, AutoModel


def tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} is not installed")
    return path


def displayed_size(video: Path) -> tuple[int, int]:
    """Width/height after rotation (ffmpeg auto-rotates when decoding)."""
    out = subprocess.run(  # noqa: S603
        [
            tool("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:stream_side_data=rotation",
            "-of",
            "json",
            str(video),
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    st = json.loads(out)["streams"][0]
    w, h = int(st["width"]), int(st["height"])
    rot = next((int(sd.get("rotation", 0)) for sd in st.get("side_data_list", [])), 0)
    return (h, w) if abs(rot) % 180 == 90 else (w, h)


STEP = 0.2
EXTRACTOR_VERSION = "dinov2s-cls-crop+logband32-v1"
MODEL_ID = "facebook/dinov2-small"
SIDE = 224
AUDIO_RATE = 16_000
N_BANDS = 32

F32 = npt.NDArray[np.float32]


def _crop_box(
    play_area: Sequence[Sequence[float]] | None, w: int, h: int
) -> tuple[int, int, int, int]:
    if not play_area:
        return 0, 0, w, h
    xs = [p[0] for p in play_area]
    ys = [p[1] for p in play_area]
    x0, x1 = max(0.0, min(xs) - 0.03), min(1.0, max(xs) + 0.03)
    y0, y1 = max(0.0, min(ys) - 0.05), min(1.0, max(ys) + 0.03)
    cw, ch = int((x1 - x0) * w) // 2 * 2, int((y1 - y0) * h) // 2 * 2
    return int(x0 * w), int(y0 * h), cw, ch


def video_embeddings(
    video: Path, play_area: Sequence[Sequence[float]] | None, batch: int = 64
) -> F32:
    width, height = displayed_size(video)
    x, y, cw, ch = _crop_box(play_area, width, height)
    vf = f"fps={1 / STEP:g},crop={cw}:{ch}:{x}:{y},scale={SIDE}:{SIDE}:flags=area"
    proc = subprocess.Popen(  # noqa: S603
        [
            tool("ffmpeg"),
            "-v",
            "error",
            "-i",
            str(video),
            "-an",
            "-vf",
            vf,
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
    )
    processor = AutoImageProcessor.from_pretrained(MODEL_ID)
    model = AutoModel.from_pretrained(MODEL_ID).eval()
    mean = torch.tensor(processor.image_mean).view(1, 3, 1, 1)
    std = torch.tensor(processor.image_std).view(1, 3, 1, 1)
    out: list[F32] = []
    frame_bytes = SIDE * SIDE * 3
    assert proc.stdout is not None
    with torch.inference_mode():
        while True:
            buf = proc.stdout.read(frame_bytes * batch)
            if len(buf) < frame_bytes:
                break
            n = len(buf) // frame_bytes
            arr = np.frombuffer(buf[: n * frame_bytes], np.uint8).reshape(n, SIDE, SIDE, 3)
            t = torch.from_numpy(arr.copy()).permute(0, 3, 1, 2).float() / 255.0
            t = (t - mean) / std
            cls = model(pixel_values=t).last_hidden_state[:, 0]
            out.append(cls.numpy().astype(np.float32))
    proc.wait()
    return np.concatenate(out)


def audio_bands(video: Path, n_steps: int) -> F32:
    raw = subprocess.run(  # noqa: S603
        [
            tool("ffmpeg"),
            "-v",
            "error",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(AUDIO_RATE),
            "-f",
            "f32le",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
    ).stdout
    sig = np.frombuffer(raw, np.float32)
    hop = int(AUDIO_RATE * STEP)
    edges = np.geomspace(60, AUDIO_RATE / 2, N_BANDS + 1)
    freqs = np.fft.rfftfreq(hop, 1 / AUDIO_RATE)
    band_of = np.clip(np.searchsorted(edges, freqs) - 1, 0, N_BANDS - 1)
    feats = np.full((n_steps, N_BANDS), -12.0, np.float32)
    win = np.hanning(hop).astype(np.float32)
    for i in range(min(n_steps, len(sig) // hop)):
        spec = np.abs(np.fft.rfft(sig[i * hop : (i + 1) * hop] * win)) ** 2
        energy = np.bincount(band_of, weights=spec, minlength=N_BANDS)
        feats[i] = np.log(energy + 1e-6)
    return feats


def extract(video: Path, play_area: Sequence[Sequence[float]] | None, cache_dir: Path) -> F32:
    """[n_steps, 384 + 32] features, cached."""
    st = video.stat()
    key = f"{video.stem[:40]}-{st.st_size}-{int(st.st_mtime)}-{EXTRACTOR_VERSION}"
    cache = cache_dir / f"{key}.npz"
    if cache.exists():
        return np.load(cache)["x"]
    vid = video_embeddings(video, play_area)
    aud = audio_bands(video, len(vid))
    x = np.concatenate([vid, aud], axis=1).astype(np.float32)
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(cache, x=x)
    return x
