"""Ingest: validate an upload and make its playback proxy + poster.

Steps: download original -> sha256 -> ffprobe (reject non-videos) -> 720p H.264 proxy ->
poster -> upload both -> record metadata and mark the video ready (atomically with the job's
success) -> delete the original to stay inside the storage free tier (unless keep_originals).
"""

from __future__ import annotations

import hashlib
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, update
from sqlalchemy.orm import Session

from vball_core import pipelines
from vball_core.db.enums import VideoStatus
from vball_core.db.models import Video
from vball_core.logging import get_logger
from vball_core.settings import Settings
from vball_core.storage import ObjectStorage
from vball_worker.context import JobContext, JobResult
from vball_worker.media import InvalidMediaError, extract_poster, probe, transcode_proxy

PIPELINE_VERSION = pipelines.INGEST
log = get_logger("vball_worker.ingest")


def proxy_key(video: Video) -> str:
    return f"proxies/{video.owner_id}/{video.id}.mp4"


def poster_key(video: Video) -> str:
    return f"posters/{video.owner_id}/{video.id}.jpg"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_ingest(ctx: JobContext, settings: Settings, storage: ObjectStorage) -> JobResult:
    with Session(ctx.engine, expire_on_commit=False) as session:
        video = session.get(Video, ctx.job.video_id)
        if video is not None:
            session.expunge(video)
    if video is None or video.status == VideoStatus.DELETED:
        raise InvalidMediaError("The video was deleted.")
    if video.original_deleted_at is not None:
        raise InvalidMediaError("The original upload is no longer available.")

    with tempfile.TemporaryDirectory(dir=settings.worker_scratch_dir, prefix="ingest-") as tmp:
        work = Path(tmp)
        src = work / ("original" + Path(video.original_key).suffix)
        proxy = work / "proxy.mp4"
        poster = work / "poster.jpg"

        with ctx.stage("download"):
            storage.download(video.original_key, str(src))
            if src.stat().st_size != video.size_bytes:
                raise InvalidMediaError("The stored upload is incomplete.")
            digest = _sha256(src)
        ctx.progress(0.05)

        with ctx.stage("probe"):
            info = probe(src)
            if info.duration_s > settings.max_video_duration_s:
                hours = settings.max_video_duration_s / 3600
                raise InvalidMediaError(f"Videos longer than {hours:g} hours aren't supported.")
        ctx.progress(0.08)

        with ctx.stage("transcode"):
            transcode_proxy(
                src,
                proxy,
                info,
                settings.proxy_height,
                on_progress=lambda p: ctx.progress(0.08 + 0.85 * p),
            )
        with ctx.stage("poster"):
            extract_poster(proxy, poster, min(info.duration_s * 0.1, 30.0))
        ctx.progress(0.95)

        with ctx.stage("upload"):
            storage.upload(str(proxy), proxy_key(video), "video/mp4")
            storage.upload(str(poster), poster_key(video), "image/jpeg")
            proxy_size = proxy.stat().st_size
        ctx.progress(0.98)

    fps = info.fps.limit_denominator(1001)

    def finalize(conn: Connection) -> None:
        conn.execute(
            update(Video)
            .where(Video.id == video.id)
            .values(
                status=VideoStatus.READY,
                sha256=digest,
                duration_s=info.duration_s,
                fps_num=fps.numerator,
                fps_den=fps.denominator,
                width=info.width,
                height=info.height,
                rotation=info.rotation,
                video_codec=info.video_codec[:32],
                audio_codec=info.audio_codec[:32] if info.audio_codec else None,
                container=info.container,
                recorded_at=info.recorded_at,
                proxy_key=proxy_key(video),
                proxy_size_bytes=proxy_size,
                poster_key=poster_key(video),
                error_message=None,
            )
        )

    def delete_original() -> None:
        if settings.keep_originals:
            return
        try:
            storage.delete(video.original_key)
            with ctx.engine.begin() as conn:
                conn.execute(
                    update(Video)
                    .where(Video.id == video.id)
                    .values(original_deleted_at=datetime.now(UTC))
                )
            log.info("original_deleted", video_id=str(video.id))
        except Exception as exc:  # the proxy is safe; the original just stays (counts storage)
            log.warning("original_delete_failed", video_id=str(video.id), error=type(exc).__name__)

    return JobResult(finalize=finalize, after_commit=delete_original)
