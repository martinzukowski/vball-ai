"""Signals for rally detection, measured exactly like the original browser app but
deterministically (fixed timestamps from ffmpeg instead of real-time playback):

* ``act``: share of the play area whose grey level changed by > 16 since the previous sample
  (samples every 0.2 s, frames scaled to 256x144, luma = (77R + 150G + 29B) >> 8).
* ``wh`` / ``lo``: loudest level (dB) in the referee-whistle band (2-4.5 kHz) and in a low band
  (200-1500 Hz) within each 0.2 s bucket.
* ``bad`` / camera segments: the background outside the play area is compared with a reference
  frame; when it changes for good the camera moved. Samples while it moves are unreliable, and
  a new camera segment starts once the picture settles.

The play area is a polygon in normalised video coordinates (0..1) drawn by the user around the
court and the space above it. Without one the whole frame is used, which also measures people
walking in front of the camera, so results are worse.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import numpy.typing as npt

from vball_worker.media import MediaToolError, tool
from vball_worker.rally.heuristic import Features

STEP = 0.2
MW, MH = 256, 144  # motion resolution
BW, BH = 64, 36  # camera-move check resolution
DIFF_THRESHOLD = 16
BG_THRESHOLD, BG_HOLD_S, STEADY_S = 18.0, 3.0, 2.0
AUDIO_RATE = 16_000
FFT = 1024
WHISTLE_BAND = (2000.0, 4500.0)
LOW_BAND = (200.0, 1500.0)
SILENCE_DB = -200.0

Points = Sequence[Sequence[float]]
BoolArray = npt.NDArray[np.bool_]


def polygon_mask(points: Points | None, w: int, h: int) -> BoolArray:
    """Pixels whose centre lies inside the polygon (even-odd rule). None -> whole frame."""
    if not points:
        return np.ones((h, w), dtype=bool)
    ys, xs = np.mgrid[0:h, 0:w]
    px, py = (xs + 0.5) / w, (ys + 0.5) / h
    inside = np.zeros((h, w), dtype=bool)
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        if y1 == y2:
            continue
        crosses = (y1 > py) != (y2 > py)
        x_at = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
        inside ^= crosses & (px < x_at)
    return inside


def _frames(video: Path) -> subprocess.Popen[bytes]:
    cmd = [
        tool("ffmpeg"),
        "-v",
        "error",
        "-nostdin",
        "-i",
        str(video),
        "-an",
        "-vf",
        f"fps={1 / STEP:g},scale={MW}:{MH}:flags=area",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # noqa: S603


def _audio(video: Path) -> subprocess.Popen[bytes]:
    cmd = [
        tool("ffmpeg"),
        "-v",
        "error",
        "-nostdin",
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
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)  # noqa: S603


def motion_signals(
    video: Path,
    play_area: Points | None,
    expected_samples: int,
    on_progress: Callable[[float], None] | None = None,
) -> tuple[list[float], list[float], list[int], list[float]]:
    """Returns (t, act, bad, camera segment start times)."""
    mask = polygon_mask(play_area, MW, MH)
    small_bg = ~polygon_mask(play_area, BW, BH) if play_area else None
    n_mask = int(mask.sum()) or 1
    frame_bytes = MW * MH * 3
    weights = np.array([77, 150, 29], dtype=np.uint16)

    t: list[float] = []
    act: list[float] = []
    bad: list[int] = []
    cams = [0.0]
    prev: npt.NDArray[np.int16] | None = None
    prev_small: npt.NDArray[np.float32] | None = None
    ref_small: npt.NDArray[np.float32] | None = None
    bg_high_since: float | None = None
    moved, steady_since = False, None

    proc = _frames(video)
    assert proc.stdout is not None
    k = 0
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            rgb = np.frombuffer(buf, dtype=np.uint8).reshape(MH, MW, 3)
            gray = ((rgb.astype(np.uint16) * weights).sum(axis=2) >> 8).astype(np.int16)
            small = gray.reshape(BH, MH // BH, BW, MW // BW).mean(axis=(1, 3)).astype(np.float32)
            ts = round(k * STEP, 2)
            if ref_small is None:
                ref_small = small

            sample_bad = 0
            if small_bg is not None and small_bg.any():
                bg = float(np.median(np.abs(small - ref_small)[small_bg]))
                shake = (
                    float(np.median(np.abs(small - prev_small))) if prev_small is not None else 0.0
                )
                if not moved:
                    if bg > BG_THRESHOLD:
                        bg_high_since = ts if bg_high_since is None else bg_high_since
                    else:
                        bg_high_since = None
                    if bg_high_since is not None and ts - bg_high_since >= BG_HOLD_S:
                        moved, steady_since = True, None
                        for i in range(len(t) - 1, -1, -1):  # samples since it began
                            if t[i] < bg_high_since:
                                break
                            bad[i] = 1
                if moved:
                    sample_bad = 1
                    steady_since = (
                        (steady_since if steady_since is not None else ts) if shake < 3 else None
                    )
                    if steady_since is not None and ts - steady_since >= STEADY_S:
                        # Settled in a new position: new reference, new camera segment. (The
                        # play area may no longer line up; the UI asks the user to check.)
                        ref_small, moved, bg_high_since = small, False, None
                        cams.append(ts)
                        prev = None
            a = 0.0
            if prev is not None and not sample_bad:
                changed = np.abs(gray - prev) > DIFF_THRESHOLD
                a = float(changed[mask].sum()) / n_mask
            t.append(ts)
            act.append(round(a, 4))
            bad.append(sample_bad)
            prev, prev_small = gray, small
            k += 1
            if on_progress and k % 25 == 0:
                on_progress(min(1.0, k / max(1, expected_samples)))
        code = proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    if code != 0 or not t:
        raise MediaToolError(f"could not decode video frames (exit {code})")
    return t, act, bad, cams


def audio_signals(video: Path, n_samples: int) -> tuple[list[float], list[float], bool]:
    """Per-sample loudest whistle-band and low-band level. (wh, lo, has_audio)."""
    wh = [SILENCE_DB] * n_samples
    lo = [SILENCE_DB] * n_samples
    window = np.blackman(FFT).astype(np.float32)
    freqs = np.fft.rfftfreq(FFT, 1 / AUDIO_RATE)
    w_band = (freqs >= WHISTLE_BAND[0]) & (freqs < WHISTLE_BAND[1])
    l_band = (freqs >= LOW_BAND[0]) & (freqs < LOW_BAND[1])
    hop = FFT // 2

    proc = _audio(video)
    assert proc.stdout is not None
    carry = np.zeros(0, dtype=np.float32)
    offset = 0  # sample index of carry[0]
    got_any = False
    try:
        while True:
            chunk = proc.stdout.read(AUDIO_RATE * 4 * 4)  # ~4 s of float32
            if not chunk:
                break
            got_any = True
            data = np.concatenate([carry, np.frombuffer(chunk, dtype=np.float32)])
            n_frames = max(0, (len(data) - FFT) // hop + 1)
            if n_frames:
                idx = np.arange(FFT)[None, :] + hop * np.arange(n_frames)[:, None]
                spec = np.abs(np.fft.rfft(data[idx] * window, axis=1)) / FFT
                db = 20 * np.log10(spec + 1e-12)
                w_max = db[:, w_band].max(axis=1)
                l_max = db[:, l_band].max(axis=1)
                centres = (offset + hop * np.arange(n_frames) + FFT / 2) / AUDIO_RATE
                # A sample at time t holds the loudest moment in (t - STEP, t].
                bucket = np.ceil(centres / STEP - 1e-9).astype(int)
                for b, wv, lv in zip(bucket, w_max, l_max, strict=True):
                    if 0 <= b < n_samples and wv > wh[b]:
                        wh[b], lo[b] = round(float(wv), 1), round(float(lv), 1)
                consumed = hop * n_frames
                carry, offset = data[consumed:], offset + consumed
            else:
                carry = data
        proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    return wh, lo, got_any


def extract_features(
    video: Path,
    duration_s: float,
    play_area: Points | None,
    on_progress: Callable[[float], None] | None = None,
) -> Features:
    expected = int(duration_s / STEP) + 1
    t, act, bad, cams = motion_signals(
        video, play_area, expected, (lambda p: on_progress(0.85 * p)) if on_progress else None
    )
    wh, lo, has_audio = audio_signals(video, len(t))
    if on_progress:
        on_progress(1.0)
    return Features(
        t=t, act=act, wh=wh, lo=lo, bad=bad, duration=duration_s, wh_valid=has_audio, cams=cams
    )
