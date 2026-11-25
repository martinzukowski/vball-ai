"""ffprobe parsing (pure) and the real ffmpeg pipeline on generated clips."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from mediakit import make_clip

from vball_worker.media import (
    InvalidMediaError,
    MediaInfo,
    parse_probe,
    probe,
    proxy_filters,
    transcode_proxy,
)


def probe_doc(**video: Any) -> dict[str, Any]:
    v = {
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1920,
        "height": 1080,
        "avg_frame_rate": "30000/1001",
        **video,
    }
    return {
        "format": {"duration": "65.5", "format_name": "mov,mp4,m4a"},
        "streams": [v, {"codec_type": "audio", "codec_name": "aac"}],
    }


def test_parse_basic() -> None:
    info = parse_probe(probe_doc())
    assert (info.width, info.height, info.rotation) == (1920, 1080, 0)
    assert info.fps == Fraction(30000, 1001)
    assert info.duration_s == 65.5
    assert info.audio_codec == "aac"
    assert not info.hdr


@pytest.mark.parametrize(
    ("side", "rotation", "dims"),
    [
        ([{"rotation": -90}], 270, (1080, 1920)),
        ([{"rotation": 90}], 90, (1080, 1920)),
        ([{"rotation": 180}], 180, (1920, 1080)),
        ([], 0, (1920, 1080)),
    ],
)
def test_parse_rotation(side: list[dict[str, Any]], rotation: int, dims: tuple[int, int]) -> None:
    info = parse_probe(probe_doc(side_data_list=side))
    assert info.rotation == rotation
    assert (info.width, info.height) == dims


def test_parse_hdr_and_creation_time() -> None:
    doc = probe_doc(color_transfer="arib-std-b67")
    doc["format"]["tags"] = {"com.apple.quicktime.creationdate": "2026-11-20T18:30:00-0800"}
    info = parse_probe(doc)
    assert info.hdr
    assert info.recorded_at is not None


@pytest.mark.parametrize(
    "doc",
    [
        {"format": {"duration": "10"}, "streams": [{"codec_type": "audio"}]},
        probe_doc(attached_pic=None, disposition={"attached_pic": 1}),
        {**probe_doc(), "format": {"duration": "0"}},
        probe_doc(avg_frame_rate="0/0", r_frame_rate="0/0"),
        probe_doc(width=0),
    ],
)
def test_parse_rejects_unusable(doc: dict[str, Any]) -> None:
    if "duration" in doc["format"] and doc["format"]["duration"] == "0":
        doc["streams"][0].pop("duration", None)
    with pytest.raises(InvalidMediaError):
        parse_probe(doc)


def _info(w: int, h: int, hdr: bool = False) -> MediaInfo:
    return MediaInfo(10.0, w, h, 0, Fraction(30), "h264", None, "mp4", hdr, None)


def test_filters_limit_short_side_and_never_upscale() -> None:
    assert "scale=-2:720" in proxy_filters(_info(3840, 2160), 720)
    assert "scale=720:-2" in proxy_filters(_info(1080, 1920), 720)
    assert "scale=-2:480" in proxy_filters(_info(854, 480), 720)
    assert "scale=-2:478" in proxy_filters(_info(850, 479), 720)  # even sizes only
    assert "tonemap" in proxy_filters(_info(1920, 1080, hdr=True), 720)
    assert "tonemap" not in proxy_filters(_info(1920, 1080), 720)


@pytest.mark.ffmpeg
def test_real_transcode(tmp_path: Path) -> None:
    src = make_clip(tmp_path / "in.mov", seconds=3, rotate=90)
    info = probe(src)
    assert (info.width, info.height, info.rotation) == (1080, 1920, 90)
    out = tmp_path / "proxy.mp4"
    seen: list[float] = []
    transcode_proxy(src, out, info, 720, seen.append)
    p = probe(out)
    assert (p.width, p.height, p.rotation) == (720, 1280, 0)
    assert p.video_codec == "h264"
    assert p.audio_codec == "aac"
    assert seen
    assert seen[-1] > 0.9


@pytest.mark.ffmpeg
def test_real_hdr_is_tonemapped(tmp_path: Path) -> None:
    src = make_clip(tmp_path / "hdr.mov", seconds=2, hdr=True)
    info = probe(src)
    assert info.hdr
    out = tmp_path / "proxy.mp4"
    transcode_proxy(src, out, info, 720)
    assert not probe(out).hdr


@pytest.mark.ffmpeg
def test_real_garbage_rejected(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00\x01not a video" * 1000)
    with pytest.raises(InvalidMediaError):
        probe(junk)
