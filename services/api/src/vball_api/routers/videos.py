"""Videos and their direct-to-storage multipart uploads.

Flow: POST /videos/uploads -> POST /videos/{id}/upload/parts (presigned PUT URLs, in batches)
-> browser PUTs each part to storage -> POST /videos/{id}/upload/complete. An interrupted upload
resumes by asking GET /videos/{id}/upload which parts storage already has.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Annotated, Any, Literal

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Connection, case, func, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from vball_api.deps import CurrentUser, DbSession, ensure_owner
from vball_api.wake import WakeWorker
from vball_core import jobs, pipelines
from vball_core.db.enums import ExportStatus, JobErrorCode, JobKind, JobState, VideoStatus
from vball_core.db.models import (
    AnalysisRun,
    HighlightExport,
    ModelVersion,
    ProcessingJob,
    Profile,
    Rally,
    RallyEdit,
    RallyLabelSet,
    Upload,
    Video,
)
from vball_core.logging import get_logger
from vball_core.rallies import apply_edits, compare_to_reference, overlaps
from vball_core.settings import Settings
from vball_core.storage import ObjectStorage, StorageNotConfiguredError, part_plan

router = APIRouter(prefix="/videos", tags=["videos"])
log = get_logger("vball_api.videos")

ALLOWED_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime"}
MAX_PARTS_PER_REQUEST = 100


def get_storage(request: Request) -> ObjectStorage:
    storage: ObjectStorage | None = request.app.state.storage
    if storage is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage not configured")
    return storage


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


Storage = Annotated[ObjectStorage, Depends(get_storage)]
AppSettings = Annotated[Settings, Depends(get_settings)]


# --- schemas -------------------------------------------------------------------------------
class JobOut(BaseModel):
    id: uuid.UUID
    kind: JobKind
    state: JobState
    stage: str | None
    progress: float
    attempt: int
    max_attempts: int
    error_code: JobErrorCode | None
    error_message: str | None
    created_at: datetime
    finished_at: datetime | None

    @classmethod
    def of(cls, j: ProcessingJob) -> JobOut:
        return cls(
            id=j.id,
            kind=j.kind,
            state=j.state,
            stage=j.stage,
            progress=j.progress,
            attempt=j.attempt,
            max_attempts=j.max_attempts,
            error_code=j.error_code,
            error_message=j.error_message,
            created_at=j.created_at,
            finished_at=j.finished_at,
        )


class VideoOut(BaseModel):
    id: uuid.UUID
    filename: str
    size_bytes: int
    status: VideoStatus
    created_at: datetime
    duration_s: float | None
    width: int | None
    height: int | None
    error_message: str | None
    original_available: bool
    poster_url: str | None = None
    processing: JobOut | None = None  # most recent job for this video

    @classmethod
    def of(
        cls, v: Video, job: ProcessingJob | None = None, poster_url: str | None = None
    ) -> VideoOut:
        return cls(
            id=v.id,
            filename=v.filename,
            size_bytes=v.size_bytes,
            status=v.status,
            created_at=v.created_at,
            duration_s=v.duration_s,
            width=v.width,
            height=v.height,
            error_message=v.error_message,
            original_available=v.original_deleted_at is None,
            poster_url=poster_url,
            processing=JobOut.of(job) if job else None,
        )


class CreateUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    content_type: str = Field(default="", max_length=100)

    @field_validator("filename")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        # Keep only the base name, drop control characters.
        name = PurePath(v.replace("\\", "/")).name
        name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
        if not name:
            raise ValueError("invalid file name")
        return name


class UploadPlan(BaseModel):
    video: VideoOut
    part_size_bytes: int
    part_count: int
    expires_at: datetime


class PartUrlsRequest(BaseModel):
    part_numbers: list[int] = Field(min_length=1, max_length=MAX_PARTS_PER_REQUEST)


class PartUrl(BaseModel):
    part_number: int
    url: str


class PartUrls(BaseModel):
    urls: list[PartUrl]
    expires_in_s: int


class UploadedPartOut(BaseModel):
    part_number: int
    size_bytes: int


class UploadState(BaseModel):
    video: VideoOut
    part_size_bytes: int
    part_count: int
    uploaded_parts: list[UploadedPartOut]
    expires_at: datetime


class StorageUsage(BaseModel):
    used_bytes: int
    cap_bytes: int


class VideoList(BaseModel):
    videos: list[VideoOut]
    storage: StorageUsage


class PlaybackUrl(BaseModel):
    url: str
    kind: Literal["proxy", "original"]
    expires_in_s: int


# --- helpers -------------------------------------------------------------------------------
def original_key(owner_id: uuid.UUID, video_id: uuid.UUID, ext: str) -> str:
    return f"originals/{owner_id}/{video_id}{ext}"


async def stored_bytes(db: DbSession) -> int:
    """Bytes currently held in storage for all videos (single-user app: the whole bucket):
    originals not yet deleted plus playback proxies."""
    per_video = case((Video.original_deleted_at.is_(None), Video.size_bytes), else_=0) + (
        func.coalesce(Video.proxy_size_bytes, 0)
    )
    total = await db.scalar(
        select(func.coalesce(func.sum(per_video), 0)).where(Video.status != VideoStatus.DELETED)
    )
    exports = await db.scalar(
        select(func.coalesce(func.sum(HighlightExport.size_bytes), 0)).where(
            HighlightExport.status == ExportStatus.READY
        )
    )
    return int(total or 0) + int(exports or 0)


async def latest_jobs(db: DbSession, video_ids: list[uuid.UUID]) -> dict[uuid.UUID, ProcessingJob]:
    if not video_ids:
        return {}
    rows = await db.scalars(
        select(ProcessingJob)
        .where(ProcessingJob.video_id.in_(video_ids))
        .order_by(ProcessingJob.video_id, ProcessingJob.created_at.desc())
        .ext(distinct_on(ProcessingJob.video_id))
    )
    return {j.video_id: j for j in rows}


async def poster_url(storage: ObjectStorage, video: Video) -> str | None:
    if not video.poster_key:
        return None
    return await to_thread.run_sync(storage.presign_get, video.poster_key, 3600, None)


async def video_out(db: DbSession, storage: ObjectStorage, video: Video) -> VideoOut:
    job = (await latest_jobs(db, [video.id])).get(video.id)
    return VideoOut.of(video, job, await poster_url(storage, video))


async def enqueue_ingest(
    db: DbSession, video: Video, params: dict[str, object] | None = None
) -> None:
    """Queue ingest in the caller's transaction, so an upload never exists without its job."""

    def run(session: Session) -> None:
        conn: Connection = session.connection()
        jobs.enqueue(
            conn,
            video_id=video.id,
            owner_id=video.owner_id,
            kind=JobKind.INGEST,
            pipeline_version=pipelines.INGEST,
            params=dict(params or {}),
        )

    await db.run_sync(run)


async def storage_cap(db: DbSession, user: CurrentUser, settings: Settings) -> int:
    quota = await db.scalar(select(Profile.quota_storage_bytes).where(Profile.id == user.user_id))
    return min(settings.storage_cap_bytes, int(quota or 0))


async def load_owned(db: DbSession, user: CurrentUser, video_id: uuid.UUID) -> Video:
    video = await db.get(Video, video_id, with_for_update=True)
    visible = video is not None and video.status != VideoStatus.DELETED
    ensure_owner(video.owner_id if visible and video else None, user)
    assert video is not None
    return video


async def load_upload(db: DbSession, video: Video) -> Upload:
    upload = await db.scalar(select(Upload).where(Upload.video_id == video.id))
    if upload is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "video has no upload")
    return upload


def require_open(video: Video, upload: Upload) -> None:
    if video.status != VideoStatus.UPLOADING or upload.completed_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "upload already completed")
    if upload.expires_at <= datetime.now(UTC):
        raise HTTPException(status.HTTP_410_GONE, "upload expired; start a new one")


# --- routes --------------------------------------------------------------------------------
@router.get("", response_model=VideoList, operation_id="listVideos")
async def list_videos(
    user: CurrentUser, db: DbSession, settings: AppSettings, storage: Storage
) -> VideoList:
    rows = (
        await db.scalars(
            select(Video)
            .where(Video.owner_id == user.user_id, Video.status != VideoStatus.DELETED)
            .order_by(Video.created_at.desc())
        )
    ).all()
    latest = await latest_jobs(db, [v.id for v in rows])
    return VideoList(
        videos=[VideoOut.of(v, latest.get(v.id), await poster_url(storage, v)) for v in rows],
        storage=StorageUsage(
            used_bytes=await stored_bytes(db), cap_bytes=await storage_cap(db, user, settings)
        ),
    )


@router.get("/{video_id}", response_model=VideoOut, operation_id="getVideo")
async def get_video(
    video_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage
) -> VideoOut:
    return await video_out(db, storage, await load_owned(db, user, video_id))


@router.post(
    "/uploads",
    response_model=UploadPlan,
    status_code=status.HTTP_201_CREATED,
    operation_id="createUpload",
)
async def create_upload(
    body: CreateUpload, user: CurrentUser, db: DbSession, storage: Storage, settings: AppSettings
) -> UploadPlan:
    ext = PurePath(body.filename).suffix.lower()
    if ext not in ALLOWED_TYPES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "only .mp4, .m4v and .mov videos are supported"
        )
    content_type = ALLOWED_TYPES[ext]
    if body.content_type and body.content_type not in ALLOWED_TYPES.values():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "not a supported video type")
    if body.size_bytes > settings.max_upload_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "file is larger than the limit")

    cap = await storage_cap(db, user, settings)
    used = await stored_bytes(db)
    if used + body.size_bytes > cap:
        raise HTTPException(
            status.HTTP_507_INSUFFICIENT_STORAGE,
            f"not enough storage: {used} of {cap} bytes used; delete videos to make room",
        )

    part_size, part_count = part_plan(body.size_bytes)
    video_id = uuid.uuid4()
    key = original_key(user.user_id, video_id, ext)
    try:
        upload_id = await to_thread.run_sync(storage.create_multipart, key, content_type)
    except StorageNotConfiguredError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "storage not configured") from exc

    expires_at = datetime.now(UTC) + timedelta(seconds=settings.upload_expiry_s)
    video = Video(
        id=video_id,
        owner_id=user.user_id,
        filename=body.filename,
        size_bytes=body.size_bytes,
        content_type=content_type,
        original_key=key,
    )
    db.add(video)
    await db.flush()
    db.add(
        Upload(
            video_id=video_id,
            storage_upload_id=upload_id,
            part_size_bytes=part_size,
            part_count=part_count,
            expires_at=expires_at,
        )
    )
    await db.commit()
    await db.refresh(video)
    log.info("upload_created", video_id=str(video_id), size=body.size_bytes, parts=part_count)
    return UploadPlan(
        video=VideoOut.of(video),
        part_size_bytes=part_size,
        part_count=part_count,
        expires_at=expires_at,
    )


@router.get("/{video_id}/upload", response_model=UploadState, operation_id="getUploadState")
async def get_upload_state(
    video_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage
) -> UploadState:
    video = await load_owned(db, user, video_id)
    upload = await load_upload(db, video)
    parts = []
    if video.status == VideoStatus.UPLOADING and upload.completed_at is None:
        listed = await to_thread.run_sync(
            storage.list_parts, video.original_key, upload.storage_upload_id
        )
        parts = [UploadedPartOut(part_number=p.part_number, size_bytes=p.size) for p in listed]
    return UploadState(
        video=VideoOut.of(video),
        part_size_bytes=upload.part_size_bytes,
        part_count=upload.part_count,
        uploaded_parts=parts,
        expires_at=upload.expires_at,
    )


@router.post("/{video_id}/upload/parts", response_model=PartUrls, operation_id="presignParts")
async def presign_parts(
    video_id: uuid.UUID,
    body: PartUrlsRequest,
    user: CurrentUser,
    db: DbSession,
    storage: Storage,
    settings: AppSettings,
) -> PartUrls:
    video = await load_owned(db, user, video_id)
    upload = await load_upload(db, video)
    require_open(video, upload)
    numbers = sorted(set(body.part_numbers))
    if numbers[0] < 1 or numbers[-1] > upload.part_count:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "part number out of range")

    def sign_all() -> list[PartUrl]:
        return [
            PartUrl(
                part_number=n,
                url=storage.presign_part(
                    video.original_key, upload.storage_upload_id, n, settings.upload_url_ttl_s
                ),
            )
            for n in numbers
        ]

    return PartUrls(urls=await to_thread.run_sync(sign_all), expires_in_s=settings.upload_url_ttl_s)


@router.post("/{video_id}/upload/complete", response_model=VideoOut, operation_id="completeUpload")
async def complete_upload(
    video_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage, wake: WakeWorker
) -> VideoOut:
    video = await load_owned(db, user, video_id)  # row lock serialises concurrent completes
    upload = await load_upload(db, video)
    if upload.completed_at is not None:
        return await video_out(db, storage, video)  # idempotent
    require_open(video, upload)

    parts = await to_thread.run_sync(
        storage.list_parts, video.original_key, upload.storage_upload_id
    )
    have = {p.part_number for p in parts}
    missing = [n for n in range(1, upload.part_count + 1) if n not in have]
    if missing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{len(missing)} part(s) not uploaded yet, first missing: {missing[0]}",
        )
    total = sum(p.size for p in parts if p.part_number <= upload.part_count)
    if total != video.size_bytes:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"uploaded {total} bytes, expected {video.size_bytes}"
        )

    await to_thread.run_sync(
        storage.complete_multipart,
        video.original_key,
        upload.storage_upload_id,
        [p for p in parts if p.part_number <= upload.part_count],
    )
    size = await to_thread.run_sync(storage.object_size, video.original_key)
    if size != video.size_bytes:
        video.status = VideoStatus.FAILED
        video.error_message = "stored file size does not match the upload"
        await db.commit()
        log.error("upload_size_mismatch", video_id=str(video.id), stored=size)
        raise HTTPException(status.HTTP_409_CONFLICT, video.error_message)

    upload.completed_at = datetime.now(UTC)
    video.status = VideoStatus.UPLOADED
    await enqueue_ingest(db, video)
    await db.commit()
    await wake()
    await db.refresh(video)
    log.info("upload_completed", video_id=str(video.id), size=size)
    return await video_out(db, storage, video)


@router.delete("/{video_id}", status_code=status.HTTP_204_NO_CONTENT, operation_id="deleteVideo")
async def delete_video(
    video_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage
) -> Response:
    video = await load_owned(db, user, video_id)
    upload = await db.scalar(select(Upload).where(Upload.video_id == video.id))

    def remove_objects() -> None:
        if upload is not None and upload.completed_at is None:
            storage.abort_multipart(video.original_key, upload.storage_upload_id)
        for key in (video.original_key, video.proxy_key, video.poster_key):
            if key:
                storage.delete(key)

    # Stop any processing first so a worker doesn't recreate files we're about to delete.
    await db.execute(
        update(ProcessingJob)
        .where(
            ProcessingJob.video_id == video.id,
            ProcessingJob.state.in_([JobState.QUEUED, JobState.RUNNING]),
        )
        .values(cancel_requested=True)
    )
    await to_thread.run_sync(remove_objects)
    video.status = VideoStatus.DELETED
    video.deleted_at = datetime.now(UTC)
    await db.commit()
    log.info("video_deleted", video_id=str(video.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{video_id}/retry", response_model=VideoOut, operation_id="retryProcessing")
async def retry_processing(
    video_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage, wake: WakeWorker
) -> VideoOut:
    """Re-run ingest for a video whose processing failed (needs the original upload)."""
    video = await load_owned(db, user, video_id)
    if video.status != VideoStatus.FAILED:
        raise HTTPException(status.HTTP_409_CONFLICT, "only failed videos can be retried")
    if video.original_deleted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "the original upload is no longer available")
    upload = await db.scalar(select(Upload).where(Upload.video_id == video.id))
    if upload is None or upload.completed_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "the upload never finished; upload again")
    previous = await db.scalar(
        select(func.count()).where(
            ProcessingJob.video_id == video.id, ProcessingJob.kind == JobKind.INGEST
        )
    )
    video.status = VideoStatus.UPLOADED
    video.error_message = None
    await enqueue_ingest(db, video, {"retry": int(previous or 0)})
    await db.commit()
    await wake()
    await db.refresh(video)
    return await video_out(db, storage, video)


@router.get("/{video_id}/playback", response_model=PlaybackUrl, operation_id="getPlaybackUrl")
async def get_playback_url(
    video_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage
) -> PlaybackUrl:
    """Short-lived URL to stream the video: the browser-friendly proxy once it exists, else
    the original upload (which may not play in every browser, e.g. HEVC on some Androids)."""
    video = await load_owned(db, user, video_id)
    ttl = 3600
    if video.proxy_key:
        url = await to_thread.run_sync(storage.presign_get, video.proxy_key, ttl, None)
        return PlaybackUrl(url=url, kind="proxy", expires_in_s=ttl)
    if video.status in (VideoStatus.UPLOADING, VideoStatus.DELETED) or video.original_deleted_at:
        raise HTTPException(status.HTTP_409_CONFLICT, "video is not available for playback")
    url = await to_thread.run_sync(storage.presign_get, video.original_key, ttl, None)
    return PlaybackUrl(url=url, kind="original", expires_in_s=ttl)


# --- rallies -------------------------------------------------------------------------------
class RallyOut(BaseModel):
    id: uuid.UUID  # stable key for edits (a predicted rally's id, or the id of the add-edit)
    number: int
    start_t: float
    end_t: float
    serve_t: float | None
    source: Literal["model", "user"]
    edited: bool
    matches_reference: bool | None = None  # None when there is no reference to compare to


class AnalysisOut(BaseModel):
    run_id: uuid.UUID
    detector: str
    pipeline_version: str
    created_at: datetime
    sensitivity: int
    used_play_area: bool
    mode: str
    camera_moves: list[float]


class ReferenceCheck(BaseModel):
    source: str  # e.g. "balltime"
    n_reference: int
    n_ours: int
    matched: int
    f1: float
    missed: list[list[float]]  # reference rallies we didn't find, [[start, end], ...]
    # The active model was trained on this video, so the agreement above is in-sample. Then
    # held_out_f1 is the honest score from training without it (held_out_test: "gym"/"match").
    in_training: bool = False
    held_out_f1: float | None = None
    held_out_test: str | None = None


class RalliesOut(BaseModel):
    analysis: AnalysisOut | None
    rallies: list[RallyOut]
    play_area: list[list[float]] | None
    job: JobOut | None  # latest analysis job (queued/running/failed shows here)
    reviewed_at: datetime | None  # when the user last marked this video's rallies correct
    reference: ReferenceCheck | None = None  # comparison with imported reference rallies


class PlayAreaIn(BaseModel):
    points: list[list[float]] = Field(min_length=3, max_length=16)

    @field_validator("points")
    @classmethod
    def _in_frame(cls, v: list[list[float]]) -> list[list[float]]:
        for p in v:
            if len(p) != 2 or not all(-0.25 <= c <= 1.25 for c in p):
                raise ValueError("points must be [x, y] pairs in normalised coordinates")
        return [[round(p[0], 4), round(p[1], 4)] for p in v]


class AnalyzeIn(BaseModel):
    sensitivity: int = Field(default=0, ge=-5, le=5)


async def enqueue_analysis(db: DbSession, video: Video, sensitivity: int) -> None:
    if video.status != VideoStatus.READY:
        raise HTTPException(status.HTTP_409_CONFLICT, "the video isn't ready yet")
    params = {"sensitivity": sensitivity, "play_area": video.play_area}

    def run(session: Session) -> None:
        jobs.enqueue(
            session.connection(),
            video_id=video.id,
            owner_id=video.owner_id,
            kind=JobKind.ANALYZE,
            pipeline_version=pipelines.RALLY_HEURISTIC,
            params=params,
        )

    await db.run_sync(run)


async def held_out_for(
    db: DbSession, run: AnalysisRun, video_id: uuid.UUID
) -> dict[str, Any] | None:
    """The run's model's held-out result for this video, if the model was trained on it."""
    if not run.model_version_ids:
        return None
    metrics = await db.scalar(
        select(ModelVersion.metrics).where(ModelVersion.id == run.model_version_ids[0])
    )
    if not metrics or str(video_id) not in (metrics.get("trained_on") or []):
        return None
    held = (metrics.get("held_out_by_video") or {}).get(str(video_id))
    return held if isinstance(held, dict) else {}


@router.get("/{video_id}/rallies", response_model=RalliesOut, operation_id="getRallies")
async def get_rallies(video_id: uuid.UUID, user: CurrentUser, db: DbSession) -> RalliesOut:
    video = await load_owned(db, user, video_id)
    run = await db.scalar(
        select(AnalysisRun).where(AnalysisRun.video_id == video.id, AnalysisRun.is_active)
    )
    job = await db.scalar(
        select(ProcessingJob)
        .where(ProcessingJob.video_id == video.id, ProcessingJob.kind == JobKind.ANALYZE)
        .order_by(ProcessingJob.created_at.desc())
        .limit(1)
    )
    rallies: list[RallyOut] = []
    analysis = None
    reference: ReferenceCheck | None = None
    if run is not None:
        base = (await db.scalars(select(Rally).where(Rally.analysis_run_id == run.id))).all()
        edits = (
            await db.scalars(
                select(RallyEdit)
                .where(RallyEdit.video_id == video.id)
                .order_by(RallyEdit.created_at)
            )
        ).all()
        effective = apply_edits(base, edits)
        ref_set = await db.scalar(
            select(RallyLabelSet)
            .where(
                RallyLabelSet.video_id == video.id,
                RallyLabelSet.rallies[0]["source"].astext.like("reference:%"),
            )
            .order_by(RallyLabelSet.created_at.desc())
            .limit(1)
        )
        comparison = None
        if ref_set is not None:
            source = str(ref_set.rallies[0]["source"]).split(":", 1)[1]
            comparison = compare_to_reference(
                effective,
                [(float(x["start_t"]), float(x["end_t"])) for x in ref_set.rallies],
                source,
            )
            held = await held_out_for(db, run, video.id)
            reference = ReferenceCheck(
                in_training=held is not None,
                held_out_f1=held.get("f1") if held else None,
                held_out_test=held.get("held_out") if held else None,
                source=source,
                n_reference=comparison.n_reference,
                n_ours=comparison.n_ours,
                matched=comparison.matched,
                f1=comparison.f1,
                missed=[[s0, e0] for s0, e0 in comparison.missed],
            )
        rallies = [
            RallyOut(
                id=r.key,
                number=i + 1,
                start_t=r.start_t,
                end_t=r.end_t,
                serve_t=r.serve_t,
                source=r.source,
                edited=r.edited,
                matches_reference=(r.key in comparison.matched_keys) if comparison else None,
            )
            for i, r in enumerate(effective)
        ]
        p = run.params
        analysis = AnalysisOut(
            run_id=run.id,
            detector=str(p.get("detector", "")),
            pipeline_version=run.pipeline_version,
            created_at=run.created_at,
            sensitivity=int(p.get("sensitivity", 0)),
            used_play_area=bool(p.get("play_area")),
            mode=str(p.get("mode", "")),
            camera_moves=[float(x) for x in p.get("camera_moves", [])],
        )
    reviewed_at = await db.scalar(
        select(func.max(RallyLabelSet.created_at)).where(RallyLabelSet.video_id == video.id)
    )
    return RalliesOut(
        analysis=analysis,
        rallies=rallies,
        play_area=video.play_area,
        job=JobOut.of(job) if job else None,
        reviewed_at=reviewed_at,
        reference=reference,
    )


@router.put("/{video_id}/play-area", response_model=RalliesOut, operation_id="setPlayArea")
async def set_play_area(
    video_id: uuid.UUID, body: PlayAreaIn, user: CurrentUser, db: DbSession, wake: WakeWorker
) -> RalliesOut:
    """Save the play area and re-run rally detection with it."""
    video = await load_owned(db, user, video_id)
    video.play_area = body.points
    await enqueue_analysis(db, video, 0)
    await db.commit()
    await wake()
    return await get_rallies(video_id, user, db)


@router.post("/{video_id}/analyze", response_model=RalliesOut, operation_id="analyzeVideo")
async def analyze_video(
    video_id: uuid.UUID, body: AnalyzeIn, user: CurrentUser, db: DbSession, wake: WakeWorker
) -> RalliesOut:
    """Re-run rally detection, e.g. with a different sensitivity (-5 fewer .. 5 more)."""
    video = await load_owned(db, user, video_id)
    await enqueue_analysis(db, video, body.sensitivity)
    await db.commit()
    await wake()
    return await get_rallies(video_id, user, db)


class RallyEditIn(BaseModel):
    op: Literal["add", "modify", "delete"]
    rally_id: uuid.UUID | None = None  # required for modify/delete
    start_t: float | None = Field(default=None, ge=0)
    end_t: float | None = Field(default=None, gt=0)


async def _effective(db: DbSession, video: Video) -> tuple[AnalysisRun, list[Any]]:
    run = await db.scalar(
        select(AnalysisRun).where(AnalysisRun.video_id == video.id, AnalysisRun.is_active)
    )
    if run is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "rallies haven't been detected yet")
    base = (await db.scalars(select(Rally).where(Rally.analysis_run_id == run.id))).all()
    edits = (
        await db.scalars(
            select(RallyEdit).where(RallyEdit.video_id == video.id).order_by(RallyEdit.created_at)
        )
    ).all()
    return run, apply_edits(base, edits)


@router.post("/{video_id}/rallies/edits", response_model=RalliesOut, operation_id="editRally")
async def edit_rally(
    video_id: uuid.UUID, body: RallyEditIn, user: CurrentUser, db: DbSession
) -> RalliesOut:
    """Trim, add or delete a rally. Predictions are kept; edits are layered on top."""
    video = await load_owned(db, user, video_id)
    _, current = await _effective(db, video)
    edit = RallyEdit(video_id=video.id, user_id=user.user_id, op=body.op)
    if body.op != "add":
        target = next((r for r in current if r.key == body.rally_id), None)
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "rally not found")
        if target.source == "model":
            edit.base_rally_id = target.key
        else:
            edit.base_edit_id = target.key
    if body.op != "delete":
        if body.start_t is None or body.end_t is None or body.end_t - body.start_t < 0.5:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "a rally needs a start and an end"
            )
        if video.duration_s and body.end_t > video.duration_s + 0.5:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "end is past the video")
        if overlaps(current, body.start_t, body.end_t, body.rally_id):
            raise HTTPException(status.HTTP_409_CONFLICT, "rallies can't overlap")
        edit.start_t, edit.end_t = round(body.start_t, 2), round(body.end_t, 2)
    db.add(edit)
    await db.commit()
    return await get_rallies(video_id, user, db)


@router.post("/{video_id}/review", response_model=RalliesOut, operation_id="markReviewed")
async def mark_reviewed(video_id: uuid.UUID, user: CurrentUser, db: DbSession) -> RalliesOut:
    """Freeze the current (corrected) rallies as ground truth for training and evaluation."""
    video = await load_owned(db, user, video_id)
    run, current = await _effective(db, video)
    rallies = [
        {
            "start_t": r.start_t,
            "end_t": r.end_t,
            "source": r.source,
            "edited": r.edited,
            "predicted_start_t": r.predicted_start_t,
            "predicted_end_t": r.predicted_end_t,
        }
        for r in current
    ]
    db.add(
        RallyLabelSet(
            video_id=video.id,
            user_id=user.user_id,
            base_run_id=run.id,
            duration_s=video.duration_s or max((r.end_t for r in current), default=1.0),
            rallies=rallies,
            n_rallies=len(rallies),
        )
    )
    await db.commit()
    log.info("rallies_reviewed", video_id=str(video.id), n=len(rallies))
    return await get_rallies(video_id, user, db)
