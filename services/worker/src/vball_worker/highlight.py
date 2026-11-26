"""Highlight export: cut each chosen clip from the playback proxy (re-encoded, so cuts are
frame-accurate rather than snapping to keyframes) and join them into one MP4."""

from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import Connection, update
from sqlalchemy.orm import Session

from vball_core.db.enums import ExportStatus
from vball_core.db.models import HighlightExport, Video
from vball_core.settings import Settings
from vball_core.storage import ObjectStorage
from vball_worker.context import JobContext, JobResult
from vball_worker.media import InvalidMediaError, MediaToolError, tool


def _run(cmd: list[str]) -> None:
    out = subprocess.run(cmd, capture_output=True, timeout=900, check=False)  # noqa: S603
    if out.returncode != 0:
        raise MediaToolError(f"ffmpeg failed: {out.stderr.decode(errors='replace')[-800:]}")


def run_highlight(ctx: JobContext, settings: Settings, storage: ObjectStorage) -> JobResult:
    export_id = uuid.UUID(str(ctx.job.params["export_id"]))
    with Session(ctx.engine, expire_on_commit=False) as session:
        ex = session.get(HighlightExport, export_id)
        video = session.get(Video, ctx.job.video_id)
        if ex is not None:
            session.expunge(ex)
        if video is not None:
            session.expunge(video)
    if ex is None or ex.status == ExportStatus.EXPIRED:
        raise InvalidMediaError("The export was deleted.")
    if video is None or not video.proxy_key:
        raise InvalidMediaError("The video is no longer available.")
    clips = [(float(s), float(e)) for s, e in ex.clips]
    total = sum(e - s for s, e in clips) or 1.0
    key = f"highlights/{ex.owner_id}/{ex.id}.mp4"

    with tempfile.TemporaryDirectory(dir=settings.worker_scratch_dir, prefix="highlight-") as tmp:
        work = Path(tmp)
        src = work / "proxy.mp4"
        with ctx.stage("download"):
            storage.download(video.proxy_key, str(src))
        ctx.progress(0.05)
        parts: list[Path] = []
        done = 0.0
        with ctx.stage("cut"):
            for i, (s, e) in enumerate(clips):
                part = work / f"part{i:04d}.mp4"
                # Input seeking + re-encode: fast and frame-accurate. Uniform encode settings so
                # the concat demuxer can join parts without re-encoding again.
                _run(
                    [
                        tool("ffmpeg"),
                        "-v",
                        "error",
                        "-nostdin",
                        "-y",
                        "-ss",
                        f"{s:.3f}",
                        "-i",
                        str(src),
                        "-t",
                        f"{e - s:.3f}",
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "22",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                        "-ac",
                        "2",
                        "-ar",
                        "48000",
                        "-video_track_timescale",
                        "90000",
                        str(part),
                    ]
                )
                parts.append(part)
                done += e - s
                ctx.progress(0.05 + 0.85 * done / total)
        out = work / "highlights.mp4"
        with ctx.stage("join"):
            listing = work / "parts.txt"
            listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
            _run(
                [
                    tool("ffmpeg"),
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(listing),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(out),
                ]
            )
        with ctx.stage("upload"):
            storage.upload(str(out), key, "video/mp4")
            size = out.stat().st_size

    def finalize(conn: Connection) -> None:
        conn.execute(
            update(HighlightExport)
            .where(HighlightExport.id == ex.id)
            .values(status=ExportStatus.READY, output_key=key, size_bytes=size)
        )

    return JobResult(finalize=finalize)
