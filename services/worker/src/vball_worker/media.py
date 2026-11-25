"""ffprobe / ffmpeg wrappers: validate uploads, make the browser-friendly playback proxy and a
poster frame. Everything runs as subprocesses with argument lists (never a shell string)."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}  # HLG (iPhone default), PQ
PROBE_TIMEOUT_S = 120


class InvalidMediaError(Exception):
    """The file is not a usable video. The message is shown to the user."""


class MediaToolError(RuntimeError):
    """ffmpeg/ffprobe failed on a file that probed fine (retryable as an internal error)."""


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration_s: float
    width: int  # as displayed, i.e. after applying rotation
    height: int
    rotation: int  # 0, 90, 180, 270
    fps: Fraction
    video_codec: str
    audio_codec: str | None
    container: str
    hdr: bool
    recorded_at: datetime | None


def tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MediaToolError(f"{name} is not installed")
    return path


def _rotation(stream: dict[str, Any]) -> int:
    raw: Any = None
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            raw = sd["rotation"]
    if raw is None:
        raw = (stream.get("tags") or {}).get("rotate")
    if raw is None:
        return 0
    try:
        deg = float(raw)
    except (TypeError, ValueError):
        return 0
    return int(round(deg / 90.0) * 90) % 360


def _fps(stream: dict[str, Any]) -> Fraction:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key) or "0/0"
        try:
            num, den = (int(x) for x in str(raw).split("/"))
        except ValueError:
            continue
        if num > 0 and den > 0:
            return Fraction(num, den)
    return Fraction(0)


def _recorded_at(fmt: dict[str, Any], stream: dict[str, Any]) -> datetime | None:
    for tags in (fmt.get("tags") or {}, stream.get("tags") or {}):
        raw = tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time")
        if raw:
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def _as_float(raw: Any) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def parse_probe(data: dict[str, Any]) -> MediaInfo:
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    videos = [
        s
        for s in streams
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    if not videos:
        raise InvalidMediaError("The file has no video track.")
    v = videos[0]
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = max((_as_float(x) for x in (fmt.get("duration"), v.get("duration"))), default=0.0)
    if duration <= 0:
        raise InvalidMediaError("The video has no playable length.")
    fps = _fps(v)
    if fps <= 0:
        raise InvalidMediaError("Couldn't read the video's frame rate.")
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if w <= 0 or h <= 0:
        raise InvalidMediaError("Couldn't read the video's dimensions.")
    rotation = _rotation(v)
    if rotation in (90, 270):
        w, h = h, w
    return MediaInfo(
        duration_s=duration,
        width=w,
        height=h,
        rotation=rotation,
        fps=fps,
        video_codec=str(v.get("codec_name") or "unknown"),
        audio_codec=str(audio.get("codec_name")) if audio else None,
        container=str(fmt.get("format_name") or "unknown")[:64],
        hdr=str(v.get("color_transfer") or "") in HDR_TRANSFERS,
        recorded_at=_recorded_at(fmt, v),
    )


def probe(path: Path) -> MediaInfo:
    cmd = [
        tool("ffprobe"),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, capture_output=True, timeout=PROBE_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise InvalidMediaError("The file took too long to read; it may be damaged.") from exc
    if out.returncode != 0:
        raise InvalidMediaError("This file couldn't be read as a video.")
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise InvalidMediaError("This file couldn't be read as a video.") from exc
    return parse_probe(data)


def proxy_filters(info: MediaInfo, max_height: int) -> str:
    """Video filter chain for the proxy. ffmpeg auto-rotates before filters, so dimensions
    here are the displayed ones."""
    chain: list[str] = []
    if info.hdr:
        # HLG/PQ (iPhone HDR) -> SDR BT.709, otherwise colours look washed out in browsers.
        chain.append(
            "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
            "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
        )
    # Limit the short side, so portrait phone videos get the same detail as landscape ones.
    short = min(info.width, info.height)
    target = min(max_height, short)
    target -= target % 2
    if info.height <= info.width:
        chain.append(f"scale=-2:{target}:flags=bicubic")
    else:
        chain.append(f"scale={target}:-2:flags=bicubic")
    chain.append("format=yuv420p")
    return ",".join(chain)


def proxy_command(src: Path, dst: Path, info: MediaInfo, max_height: int) -> list[str]:
    return [
        tool("ffmpeg"),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        proxy_filters(info, max_height),
        "-fpsmax",
        "60",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "26",
        "-profile:v",
        "high",
        # A keyframe every 2 s keeps seeking in the review timeline snappy.
        "-force_key_frames",
        "expr:gte(t,n_forced*2)",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ac",
        "2",
        # Drop source metadata (GPS location etc.) from the copy we store.
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(dst),
    ]


def transcode_proxy(
    src: Path,
    dst: Path,
    info: MediaInfo,
    max_height: int,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Run ffmpeg, reporting progress 0..1. ``on_progress`` may raise to abort (cancellation);
    ffmpeg is then killed and the exception propagates."""
    stderr_path = dst.with_suffix(".ffmpeg.log")
    with stderr_path.open("wb") as err:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            proxy_command(src, dst, info, max_height),
            stdout=subprocess.PIPE,
            stderr=err,
            text=True,
        )
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and on_progress and value.isdigit():
                    on_progress(min(1.0, int(value) / 1e6 / info.duration_s))
            code = proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
    if code != 0 or not dst.exists() or dst.stat().st_size == 0:
        tail = stderr_path.read_text(errors="replace")[-1500:]
        raise MediaToolError(f"ffmpeg failed (exit {code}): {tail}")


def extract_poster(src: Path, dst: Path, at_s: float) -> None:
    cmd = [
        tool("ffmpeg"),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        f"{max(0.0, at_s):.3f}",
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-vf",
        "scale=-2:360",
        "-q:v",
        "4",
        str(dst),
    ]
    out = subprocess.run(cmd, capture_output=True, timeout=120, check=False)  # noqa: S603
    if out.returncode != 0 or not dst.exists():
        raise MediaToolError("could not extract a poster frame")
