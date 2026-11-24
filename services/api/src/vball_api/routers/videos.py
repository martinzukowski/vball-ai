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
from typing import Annotated, Literal

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from vball_api.deps import CurrentUser, DbSession, ensure_owner
from vball_core.db.enums import VideoStatus
from vball_core.db.models import Profile, Upload, Video
from vball_core.logging import get_logger
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

    @classmethod
    def of(cls, v: Video) -> VideoOut:
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
    """Bytes currently held in storage for all videos (single-user app: the whole bucket)."""
    total = await db.scalar(
        select(func.coalesce(func.sum(Video.size_bytes), 0)).where(
            Video.status != VideoStatus.DELETED, Video.original_deleted_at.is_(None)
        )
    )
    return int(total or 0)


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
async def list_videos(user: CurrentUser, db: DbSession, settings: AppSettings) -> VideoList:
    rows = (
        await db.scalars(
            select(Video)
            .where(Video.owner_id == user.user_id, Video.status != VideoStatus.DELETED)
            .order_by(Video.created_at.desc())
        )
    ).all()
    return VideoList(
        videos=[VideoOut.of(v) for v in rows],
        storage=StorageUsage(
            used_bytes=await stored_bytes(db), cap_bytes=await storage_cap(db, user, settings)
        ),
    )


@router.get("/{video_id}", response_model=VideoOut, operation_id="getVideo")
async def get_video(video_id: uuid.UUID, user: CurrentUser, db: DbSession) -> VideoOut:
    return VideoOut.of(await load_owned(db, user, video_id))


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
    video_id: uuid.UUID, user: CurrentUser, db: DbSession, storage: Storage
) -> VideoOut:
    video = await load_owned(db, user, video_id)  # row lock serialises concurrent completes
    upload = await load_upload(db, video)
    if upload.completed_at is not None:
        return VideoOut.of(video)  # idempotent
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
    await db.commit()
    await db.refresh(video)
    log.info("upload_completed", video_id=str(video.id), size=size)
    return VideoOut.of(video)


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

    await to_thread.run_sync(remove_objects)
    video.status = VideoStatus.DELETED
    video.deleted_at = datetime.now(UTC)
    await db.commit()
    log.info("video_deleted", video_id=str(video.id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
