"""Phase 1 database schema.

Design rules (see ARCHITECTURE.md §5):

* Every analysis result belongs to an ``analysis_runs`` row, which records the pipeline and
  model versions that produced it.
* Predictions (``rallies``, ``camera_segments``) are never mutated after a run succeeds. User
  corrections live in ``rally_edits`` and are applied on read.
* Bulk per-frame data lives in object storage; rows only hold keys and hashes.
* ``profiles.id`` equals the Supabase Auth user id (JWT ``sub``). There is deliberately no
  foreign key into Supabase's ``auth`` schema so the schema also runs on plain Postgres.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from vball_core.db.base import Base
from vball_core.db.enums import (
    CalibrationSource,
    EditOp,
    ExportStatus,
    JobErrorCode,
    JobKind,
    JobState,
    ModelStatus,
    ModelTask,
    RunStatus,
    VideoStatus,
)

SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


def text_enum(enum_cls: type[enum.Enum], name: str) -> sa.Enum:
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def created_at() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def updated_at() -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


def fk(target: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> Mapped[Any]:
    return mapped_column(
        UUID(as_uuid=True), sa.ForeignKey(target, ondelete=ondelete), nullable=nullable, index=True
    )


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(sa.String(100))
    quota_storage_bytes: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, server_default=sa.text(str(50 * 1024**3))
    )
    quota_minutes_month: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("600")
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    __table_args__ = (
        sa.CheckConstraint("quota_storage_bytes >= 0", name="quota_storage_nonneg"),
        sa.CheckConstraint("quota_minutes_month >= 0", name="quota_minutes_nonneg"),
    )


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[uuid.UUID] = uuid_pk()
    owner_id: Mapped[uuid.UUID] = fk("profiles.id")
    status: Mapped[VideoStatus] = mapped_column(
        text_enum(VideoStatus, "video_status"),
        nullable=False,
        server_default=VideoStatus.UPLOADING.value,
    )
    filename: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    sha256: Mapped[str | None] = mapped_column(sa.String(64))

    original_key: Mapped[str] = mapped_column(sa.String(512), nullable=False, unique=True)
    proxy_key: Mapped[str | None] = mapped_column(sa.String(512), unique=True)
    poster_key: Mapped[str | None] = mapped_column(sa.String(512))
    original_deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    # Filled by the ingest job from ffprobe; NULL until then.
    duration_s: Mapped[float | None] = mapped_column(sa.Double)
    fps_num: Mapped[int | None] = mapped_column(sa.Integer)
    fps_den: Mapped[int | None] = mapped_column(sa.Integer)
    width: Mapped[int | None] = mapped_column(sa.Integer)
    height: Mapped[int | None] = mapped_column(sa.Integer)
    rotation: Mapped[int | None] = mapped_column(sa.SmallInteger)
    video_codec: Mapped[str | None] = mapped_column(sa.String(32))
    audio_codec: Mapped[str | None] = mapped_column(sa.String(32))
    container: Mapped[str | None] = mapped_column(sa.String(64))
    recorded_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    error_message: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.CheckConstraint("size_bytes > 0", name="size_positive"),
        sa.CheckConstraint("duration_s IS NULL OR duration_s > 0", name="duration_positive"),
        sa.CheckConstraint(
            "num_nulls(fps_num, fps_den) <> 1 AND coalesce(fps_num > 0 AND fps_den > 0, true)",
            name="fps_valid",
        ),
        sa.CheckConstraint(
            "num_nulls(width, height) <> 1 AND coalesce(width > 0 AND height > 0, true)",
            name="dimensions_valid",
        ),
        sa.CheckConstraint("rotation IS NULL OR rotation IN (0, 90, 180, 270)", name="rotation"),
        sa.CheckConstraint(f"sha256 IS NULL OR sha256 {SHA256_CHECK}", name="sha256_hex"),
        sa.CheckConstraint(
            "status <> 'ready' OR (duration_s IS NOT NULL AND proxy_key IS NOT NULL)",
            name="ready_has_media",
        ),
        sa.CheckConstraint(
            "(status = 'deleted') = (deleted_at IS NOT NULL)", name="deleted_consistent"
        ),
        sa.Index("ix_videos_owner_created", "owner_id", sa.text("created_at DESC")),
    )

    upload: Mapped[Upload | None] = relationship(back_populates="video", cascade="all")


class Upload(Base):
    """State of an S3 multipart upload, so an interrupted upload can be resumed."""

    __tablename__ = "uploads"

    id: Mapped[uuid.UUID] = uuid_pk()
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    storage_upload_id: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    part_size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    part_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = created_at()

    __table_args__ = (
        # S3/R2 multipart limits: parts of 5 MiB..5 GiB, at most 10,000 parts.
        sa.CheckConstraint(
            "part_size_bytes BETWEEN 5242880 AND 5368709120", name="part_size_range"
        ),
        sa.CheckConstraint("part_count BETWEEN 1 AND 10000", name="part_count_range"),
    )

    video: Mapped[Video] = relationship(back_populates="upload")


class ProcessingJob(Base):
    """Durable job queue row. Workers claim with ``FOR UPDATE SKIP LOCKED`` and hold a lease."""

    __tablename__ = "processing_jobs"

    id: Mapped[uuid.UUID] = uuid_pk()
    video_id: Mapped[uuid.UUID] = fk("videos.id")
    owner_id: Mapped[uuid.UUID] = fk("profiles.id")
    kind: Mapped[JobKind] = mapped_column(text_enum(JobKind, "job_kind"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    pipeline_version: Mapped[str] = mapped_column(sa.String(64), nullable=False)

    state: Mapped[JobState] = mapped_column(
        text_enum(JobState, "job_state"), nullable=False, server_default=JobState.QUEUED.value
    )
    stage: Mapped[str | None] = mapped_column(sa.String(64))
    progress: Mapped[float] = mapped_column(sa.Double, nullable=False, server_default="0")
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="3")
    run_after: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(sa.String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false()
    )

    error_code: Mapped[JobErrorCode | None] = mapped_column(
        text_enum(JobErrorCode, "job_error_code")
    )
    error_message: Mapped[str | None] = mapped_column(sa.Text)

    created_at: Mapped[datetime] = created_at()
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.CheckConstraint("progress >= 0 AND progress <= 1", name="progress_range"),
        sa.CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        sa.CheckConstraint("attempt >= 0 AND attempt <= max_attempts", name="attempt_range"),
        sa.CheckConstraint(
            "state <> 'running' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="running_has_lease",
        ),
        sa.CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)",
            name="finished_consistent",
        ),
        sa.CheckConstraint("state <> 'failed' OR error_code IS NOT NULL", name="failed_has_error"),
        sa.CheckConstraint(
            "state <> 'succeeded' OR (error_code IS NULL AND progress = 1)",
            name="succeeded_clean",
        ),
        # The claim query scans only runnable jobs.
        sa.Index(
            "ix_processing_jobs_runnable",
            "run_after",
            postgresql_where=sa.text("state = 'queued'"),
        ),
        sa.Index(
            "ix_processing_jobs_leases",
            "lease_expires_at",
            postgresql_where=sa.text("state = 'running'"),
        ),
    )


class JobStageMetric(Base):
    """Measured resource use per job stage; the source of real cost figures."""

    __tablename__ = "job_stage_metrics"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("processing_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    attempt: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    stage: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    wall_s: Mapped[float | None] = mapped_column(sa.Double)
    cpu_s: Mapped[float | None] = mapped_column(sa.Double)
    gpu_s: Mapped[float | None] = mapped_column(sa.Double)
    peak_mem_mb: Mapped[float | None] = mapped_column(sa.Double)
    frames_processed: Mapped[int | None] = mapped_column(sa.BigInteger)
    worker_kind: Mapped[str | None] = mapped_column(sa.String(32))
    instance_type: Mapped[str | None] = mapped_column(sa.String(64))

    __table_args__ = (
        sa.CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="time_order"),
        sa.CheckConstraint(
            "coalesce(wall_s, 0) >= 0 AND coalesce(cpu_s, 0) >= 0 AND coalesce(gpu_s, 0) >= 0",
            name="nonneg",
        ),
    )


class ModelVersion(Base):
    """Registry of trained model artifacts. Only one production model per task."""

    __tablename__ = "model_versions"

    id: Mapped[uuid.UUID] = uuid_pk()
    task: Mapped[ModelTask] = mapped_column(text_enum(ModelTask, "model_task"), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    version: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    artifact_uri: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    framework: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    input_spec: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    # Dataset versions are introduced in M9; kept as an opaque reference until then.
    training_dataset_ref: Mapped[str | None] = mapped_column(sa.String(255))
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    status: Mapped[ModelStatus] = mapped_column(
        text_enum(ModelStatus, "model_status"),
        nullable=False,
        server_default=ModelStatus.CANDIDATE.value,
    )
    created_at: Mapped[datetime] = created_at()
    promoted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    promoted_by: Mapped[str | None] = mapped_column(sa.String(100))

    __table_args__ = (
        sa.UniqueConstraint("task", "version", name="uq_model_versions_task_version"),
        sa.CheckConstraint(f"sha256 {SHA256_CHECK}", name="sha256_hex"),
        sa.CheckConstraint(
            "status <> 'production' OR promoted_at IS NOT NULL", name="production_promoted"
        ),
        sa.Index(
            "uq_model_versions_one_production",
            "task",
            unique=True,
            postgresql_where=sa.text("status = 'production'"),
        ),
    )


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    video_id: Mapped[uuid.UUID] = fk("videos.id")
    job_id: Mapped[uuid.UUID | None] = fk("processing_jobs.id", nullable=True, ondelete="SET NULL")
    pipeline_version: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    model_version_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'::uuid[]")
    )
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    status: Mapped[RunStatus] = mapped_column(
        text_enum(RunStatus, "run_status"), nullable=False, server_default=RunStatus.RUNNING.value
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.false())
    created_at: Mapped[datetime] = created_at()
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.CheckConstraint("NOT is_active OR status = 'succeeded'", name="active_succeeded"),
        sa.Index(
            "uq_analysis_runs_one_active",
            "video_id",
            unique=True,
            postgresql_where=sa.text("is_active"),
        ),
    )


class CameraSegment(Base):
    """A stretch of video with one camera position and its court calibration."""

    __tablename__ = "camera_segments"

    id: Mapped[uuid.UUID] = uuid_pk()
    analysis_run_id: Mapped[uuid.UUID] = fk("analysis_runs.id")
    start_t: Mapped[float] = mapped_column(sa.Double, nullable=False)
    end_t: Mapped[float] = mapped_column(sa.Double, nullable=False)
    source: Mapped[CalibrationSource | None] = mapped_column(
        text_enum(CalibrationSource, "calibration_source")
    )
    # {"near_left": [u, v], ...} in source-video pixels; NULL when not calibrated.
    image_keypoints: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    homography: Mapped[list[float] | None] = mapped_column(ARRAY(sa.Double))
    reprojection_err_px: Mapped[float | None] = mapped_column(sa.Double)
    confidence: Mapped[float | None] = mapped_column(sa.Double)

    __table_args__ = (
        sa.CheckConstraint("start_t >= 0 AND end_t > start_t", name="time_order"),
        sa.CheckConstraint(
            "homography IS NULL OR cardinality(homography) = 9", name="homography_3x3"
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
        sa.CheckConstraint(
            "(source IS NULL) = (image_keypoints IS NULL)", name="calibrated_has_source"
        ),
        ExcludeConstraint(
            (sa.column("analysis_run_id"), "="),
            (sa.text("numrange(start_t::numeric, end_t::numeric, '[)')"), "&&"),
            using="gist",
            name="ex_camera_segments_no_overlap",
        ),
    )


class Rally(Base):
    """A predicted rally. Immutable once its analysis run has succeeded."""

    __tablename__ = "rallies"

    id: Mapped[uuid.UUID] = uuid_pk()
    analysis_run_id: Mapped[uuid.UUID] = fk("analysis_runs.id")
    video_id: Mapped[uuid.UUID] = fk("videos.id")
    start_t: Mapped[float] = mapped_column(sa.Double, nullable=False)
    end_t: Mapped[float] = mapped_column(sa.Double, nullable=False)
    serve_t: Mapped[float | None] = mapped_column(sa.Double)
    # Calibrated probability for trained models; NULL for heuristics, whose scores are not
    # probabilities and must not be presented as such.
    confidence: Mapped[float | None] = mapped_column(sa.Double)
    detector: Mapped[str] = mapped_column(sa.String(64), nullable=False)  # e.g. "heuristic-v1"
    model_version_id: Mapped[uuid.UUID | None] = fk(
        "model_versions.id", nullable=True, ondelete="RESTRICT"
    )
    features: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    __table_args__ = (
        sa.CheckConstraint("start_t >= 0 AND end_t > start_t", name="time_order"),
        sa.CheckConstraint(
            "serve_t IS NULL OR (serve_t >= start_t AND serve_t <= end_t)", name="serve_inside"
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
        ExcludeConstraint(
            (sa.column("analysis_run_id"), "="),
            (sa.text("numrange(start_t::numeric, end_t::numeric, '[)')"), "&&"),
            using="gist",
            name="ex_rallies_no_overlap",
        ),
        sa.Index("ix_rallies_run_start", "analysis_run_id", "start_t"),
    )


class RallyEdit(Base):
    """A user correction to the rallies of a video. Append-only; later edits supersede."""

    __tablename__ = "rally_edits"

    id: Mapped[uuid.UUID] = uuid_pk()
    video_id: Mapped[uuid.UUID] = fk("videos.id")
    user_id: Mapped[uuid.UUID] = fk("profiles.id")
    base_rally_id: Mapped[uuid.UUID | None] = fk("rallies.id", nullable=True)
    # Edits on the same user-added rally chain through the edit that created it.
    base_edit_id: Mapped[uuid.UUID | None] = fk("rally_edits.id", nullable=True)
    op: Mapped[EditOp] = mapped_column(text_enum(EditOp, "edit_op"), nullable=False)
    start_t: Mapped[float | None] = mapped_column(sa.Double)
    end_t: Mapped[float | None] = mapped_column(sa.Double)
    created_at: Mapped[datetime] = created_at()
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("rally_edits.id", ondelete="SET NULL")
    )

    __table_args__ = (
        sa.CheckConstraint(
            "op = 'delete' OR (start_t IS NOT NULL AND end_t IS NOT NULL AND start_t >= 0 "
            "AND end_t > start_t)",
            name="times_for_add_modify",
        ),
        sa.CheckConstraint(
            "op <> 'delete' OR (start_t IS NULL AND end_t IS NULL)", name="no_times_for_delete"
        ),
        sa.CheckConstraint(
            "op = 'add' OR num_nonnulls(base_rally_id, base_edit_id) = 1",
            name="target_for_modify_delete",
        ),
        sa.CheckConstraint(
            "op <> 'add' OR (base_rally_id IS NULL AND base_edit_id IS NULL)",
            name="add_has_no_target",
        ),
    )


class HighlightExport(Base):
    __tablename__ = "highlight_exports"

    id: Mapped[uuid.UUID] = uuid_pk()
    video_id: Mapped[uuid.UUID] = fk("videos.id")
    owner_id: Mapped[uuid.UUID] = fk("profiles.id")
    job_id: Mapped[uuid.UUID | None] = fk("processing_jobs.id", nullable=True, ondelete="SET NULL")
    # [[start_t, end_t], ...] resolved at request time, so later edits don't change an export.
    clips: Mapped[list[list[float]]] = mapped_column(JSONB, nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    status: Mapped[ExportStatus] = mapped_column(
        text_enum(ExportStatus, "export_status"),
        nullable=False,
        server_default=ExportStatus.PENDING.value,
    )
    output_key: Mapped[str | None] = mapped_column(sa.String(512), unique=True)
    size_bytes: Mapped[int | None] = mapped_column(sa.BigInteger)
    created_at: Mapped[datetime] = created_at()
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    __table_args__ = (
        sa.CheckConstraint(
            "jsonb_typeof(clips) = 'array' AND jsonb_array_length(clips) > 0", name="clips_nonempty"
        ),
        sa.CheckConstraint(
            "status <> 'ready' OR (output_key IS NOT NULL AND coalesce(size_bytes > 0, false))",
            name="ready_has_output",
        ),
    )


ALL_TABLES = [m.__tablename__ for m in Base.__subclasses__()]
