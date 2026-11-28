"""Highlight exports: the chosen rallies cut from the video and joined into one MP4 by a
worker. Clip times are frozen when the export is requested, so later edits don't change it."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta

from anyio import to_thread
from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from vball_api.deps import CurrentUser, DbSession, ensure_owner
from vball_api.routers.videos import Storage, _effective, load_owned
from vball_api.wake import WakeWorker
from vball_core import jobs, pipelines
from vball_core.db.enums import ExportStatus, JobKind, JobState
from vball_core.db.models import HighlightExport, ProcessingJob

router = APIRouter(tags=["highlights"])
EXPIRY = timedelta(days=30)
MAX_CLIPS = 500


class CreateExport(BaseModel):
    rally_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_CLIPS)
    padding_s: float = Field(default=0.5, ge=0, le=3)
    name: str = Field(default="highlights", min_length=1, max_length=80)


class ExportOut(BaseModel):
    id: uuid.UUID
    video_id: uuid.UUID
    name: str
    status: ExportStatus
    clip_count: int
    duration_s: float
    size_bytes: int | None
    created_at: datetime
    expires_at: datetime | None
    progress: float
    error_message: str | None
    download_url: str | None


def merge_clips(spans: list[tuple[float, float]], pad: float, duration: float) -> list[list[float]]:
    """Pad each clip, clamp to the video, merge overlaps (padding can make neighbours touch)."""
    padded = sorted((max(0.0, s - pad), min(duration, e + pad)) for s, e in spans)
    out: list[list[float]] = []
    for s, e in padded:
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([round(s, 2), round(e, 2)])
    return out


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\- ]+", "", name).strip() or "highlights"
    return f"{cleaned[:80]}.mp4"


async def export_out(db: DbSession, storage: Storage, ex: HighlightExport) -> ExportOut:
    job = await db.get(ProcessingJob, ex.job_id) if ex.job_id else None
    url = None
    if ex.status == ExportStatus.READY and ex.output_key:
        url = await to_thread.run_sync(
            storage.presign_get,
            ex.output_key,
            3600,
            safe_filename(str(ex.options.get("name", "highlights"))),
        )
    error = None
    if ex.status == ExportStatus.FAILED:
        error = (job.error_message if job else None) or "Export failed."
    return ExportOut(
        id=ex.id,
        video_id=ex.video_id,
        name=str(ex.options.get("name", "highlights")),
        status=ex.status,
        clip_count=len(ex.clips),
        duration_s=round(sum(e - s for s, e in ex.clips), 1),
        size_bytes=ex.size_bytes,
        created_at=ex.created_at,
        expires_at=ex.expires_at,
        progress=1.0 if ex.status == ExportStatus.READY else (job.progress if job else 0.0),
        error_message=error,
        download_url=url,
    )


@router.post(
    "/videos/{video_id}/highlights",
    response_model=ExportOut,
    status_code=status.HTTP_201_CREATED,
    operation_id="createHighlightExport",
)
async def create_export(
    video_id: uuid.UUID,
    body: CreateExport,
    user: CurrentUser,
    db: DbSession,
    storage: Storage,
    wake: WakeWorker,
) -> ExportOut:
    video = await load_owned(db, user, video_id)
    if not video.proxy_key or not video.duration_s:
        raise HTTPException(status.HTTP_409_CONFLICT, "the video isn't ready yet")
    _, current = await _effective(db, video)
    by_id = {r.key: r for r in current}
    missing = [rid for rid in body.rally_ids if rid not in by_id]
    if missing:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "unknown rally in selection")
    clips = merge_clips(
        [(by_id[rid].start_t, by_id[rid].end_t) for rid in body.rally_ids],
        body.padding_s,
        video.duration_s,
    )
    ex = HighlightExport(
        video_id=video.id,
        owner_id=user.user_id,
        clips=clips,
        options={"name": body.name, "padding_s": body.padding_s, "source": "proxy"},
        expires_at=datetime.now(UTC) + EXPIRY,
    )
    db.add(ex)
    await db.flush()

    def queue(session: object) -> uuid.UUID:
        return jobs.enqueue(
            session.connection(),  # type: ignore[attr-defined]
            video_id=video.id,
            owner_id=user.user_id,
            kind=JobKind.HIGHLIGHT,
            pipeline_version=pipelines.HIGHLIGHT,
            params={"export_id": str(ex.id)},
        )

    ex.job_id = await db.run_sync(queue)
    await db.commit()
    await wake()
    await db.refresh(ex)
    return await export_out(db, storage, ex)


@router.get(
    "/videos/{video_id}/highlights",
    response_model=list[ExportOut],
    operation_id="listHighlightExports",
)
async def list_exports(
    video_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage
) -> list[ExportOut]:
    video = await load_owned(db, user, video_id)
    rows = (
        await db.scalars(
            select(HighlightExport)
            .where(
                HighlightExport.video_id == video.id,
                HighlightExport.status != ExportStatus.EXPIRED,
            )
            .order_by(HighlightExport.created_at.desc())
        )
    ).all()
    return [await export_out(db, storage, ex) for ex in rows]


@router.delete(
    "/highlights/{export_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="deleteHighlightExport",
)
async def delete_export(
    export_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage
) -> Response:
    ex = await db.get(HighlightExport, export_id)
    ensure_owner(ex.owner_id if ex and ex.status != ExportStatus.EXPIRED else None, user)
    assert ex is not None
    if ex.job_id:
        job = await db.get(ProcessingJob, ex.job_id)
        if job and job.state in (JobState.QUEUED, JobState.RUNNING):
            job.cancel_requested = True
    if ex.output_key:
        await to_thread.run_sync(storage.delete, ex.output_key)
    ex.status = ExportStatus.EXPIRED
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
