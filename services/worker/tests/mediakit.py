"""Generate small test videos with ffmpeg (tests needing it are marked ``ffmpeg``)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    if os.environ.get("CI"):
        pytest.fail("ffmpeg must be installed in CI")
    pytest.skip("ffmpeg not installed")


def make_clip(
    path: Path,
    *,
    seconds: float = 3,
    size: str = "1920x1080",
    rotate: int = 0,
    hdr: bool = False,
    audio: bool = True,
) -> Path:
    require_ffmpeg()
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=880"]
    cmd += ["-t", str(seconds)]
    if hdr:
        cmd += [
            "-vf",
            "format=yuv420p10le",
            "-c:v",
            "libx265",
            "-x265-params",
            "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
            "-color_primaries",
            "bt2020",
            "-color_trc",
            "arib-std-b67",
            "-colorspace",
            "bt2020nc",
            "-tag:v",
            "hvc1",
        ]
    else:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    if rotate:
        tmp = path.with_name(path.stem + ".unrotated" + path.suffix)
        subprocess.run([*cmd, str(tmp)], check=True)
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-display_rotation",
                str(rotate),
                "-i",
                str(tmp),
                "-c",
                "copy",
                str(path),
            ],
            check=True,
        )
        tmp.unlink()
    else:
        subprocess.run([*cmd, str(path)], check=True)
    return path
