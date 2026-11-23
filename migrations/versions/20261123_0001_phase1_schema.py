"""Phase 1 schema: profiles, videos, uploads, jobs, runs, rallies, edits, exports.

Revision ID: 0001
Revises: (none)
Create Date: 2026-11-23 15:20:05.592070
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen list: migrations must not import live model code, which changes over time.
TABLES = (
    "profiles",
    "videos",
    "uploads",
    "processing_jobs",
    "job_stage_metrics",
    "model_versions",
    "analysis_runs",
    "camera_segments",
    "rallies",
    "rally_edits",
    "highlight_exports",
)


def upgrade() -> None:
    # btree_gist lets the no-overlap EXCLUDE constraints combine "uuid =" with "range &&".
    # Supabase keeps extensions in the "extensions" schema; plain Postgres uses the default.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'extensions') THEN
            CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA extensions;
          ELSE
            CREATE EXTENSION IF NOT EXISTS btree_gist;
          END IF;
        END $$;
        """
    )
    op.create_table(
        "model_versions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "task",
            sa.Enum(
                "court",
                "player_detection",
                "ball",
                "rally",
                "action",
                name="model_task",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("artifact_uri", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("framework", sa.String(length=32), nullable=False),
        sa.Column(
            "input_spec",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("training_dataset_ref", sa.String(length=255), nullable=True),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "candidate",
                "production",
                "retired",
                name="model_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="candidate",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_by", sa.String(length=100), nullable=True),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_model_versions_sha256_hex")),
        sa.CheckConstraint(
            "status <> 'production' OR promoted_at IS NOT NULL",
            name=op.f("ck_model_versions_production_promoted"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_versions")),
        sa.UniqueConstraint("task", "version", name="uq_model_versions_task_version"),
    )
    op.create_index(
        "uq_model_versions_one_production",
        "model_versions",
        ["task"],
        unique=True,
        postgresql_where=sa.text("status = 'production'"),
    )
    op.create_table(
        "profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column(
            "quota_storage_bytes",
            sa.BigInteger(),
            server_default=sa.text("53687091200"),
            nullable=False,
        ),
        sa.Column(
            "quota_minutes_month", sa.Integer(), server_default=sa.text("600"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quota_minutes_month >= 0", name=op.f("ck_profiles_quota_minutes_nonneg")
        ),
        sa.CheckConstraint(
            "quota_storage_bytes >= 0", name=op.f("ck_profiles_quota_storage_nonneg")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profiles")),
    )
    op.create_table(
        "videos",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "uploading",
                "uploaded",
                "ingesting",
                "ready",
                "failed",
                "deleted",
                name="video_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="uploading",
            nullable=False,
        ),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("original_key", sa.String(length=512), nullable=False),
        sa.Column("proxy_key", sa.String(length=512), nullable=True),
        sa.Column("poster_key", sa.String(length=512), nullable=True),
        sa.Column("original_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_s", sa.Double(), nullable=True),
        sa.Column("fps_num", sa.Integer(), nullable=True),
        sa.Column("fps_den", sa.Integer(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("rotation", sa.SmallInteger(), nullable=True),
        sa.Column("video_codec", sa.String(length=32), nullable=True),
        sa.Column("audio_codec", sa.String(length=32), nullable=True),
        sa.Column("container", sa.String(length=64), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'deleted') = (deleted_at IS NOT NULL)",
            name=op.f("ck_videos_deleted_consistent"),
        ),
        sa.CheckConstraint(
            "sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_videos_sha256_hex")
        ),
        sa.CheckConstraint(
            "status <> 'ready' OR (duration_s IS NOT NULL AND proxy_key IS NOT NULL)",
            name=op.f("ck_videos_ready_has_media"),
        ),
        sa.CheckConstraint(
            "num_nulls(fps_num, fps_den) <> 1 AND coalesce(fps_num > 0 AND fps_den > 0, true)",
            name=op.f("ck_videos_fps_valid"),
        ),
        sa.CheckConstraint(
            "num_nulls(width, height) <> 1 AND coalesce(width > 0 AND height > 0, true)",
            name=op.f("ck_videos_dimensions_valid"),
        ),
        sa.CheckConstraint(
            "duration_s IS NULL OR duration_s > 0", name=op.f("ck_videos_duration_positive")
        ),
        sa.CheckConstraint(
            "rotation IS NULL OR rotation IN (0, 90, 180, 270)", name=op.f("ck_videos_rotation")
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_videos_size_positive")),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["profiles.id"],
            name=op.f("fk_videos_owner_id_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_videos")),
        sa.UniqueConstraint("original_key", name=op.f("uq_videos_original_key")),
        sa.UniqueConstraint("proxy_key", name=op.f("uq_videos_proxy_key")),
    )
    op.create_index(
        "ix_videos_owner_created",
        "videos",
        ["owner_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(op.f("ix_videos_owner_id"), "videos", ["owner_id"], unique=False)
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("video_id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "ingest",
                "analyze",
                "redetect",
                "highlight",
                name="job_kind",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("pipeline_version", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                name="job_state",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("progress", sa.Double(), server_default="0", nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "error_code",
            sa.Enum(
                "input_invalid",
                "transient",
                "internal",
                "cancelled",
                name="job_error_code",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(state IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL)",
            name=op.f("ck_processing_jobs_finished_consistent"),
        ),
        sa.CheckConstraint(
            "state <> 'failed' OR error_code IS NOT NULL",
            name=op.f("ck_processing_jobs_failed_has_error"),
        ),
        sa.CheckConstraint(
            "state <> 'running' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_processing_jobs_running_has_lease"),
        ),
        sa.CheckConstraint(
            "state <> 'succeeded' OR (error_code IS NULL AND progress = 1)",
            name=op.f("ck_processing_jobs_succeeded_clean"),
        ),
        sa.CheckConstraint(
            "attempt >= 0 AND attempt <= max_attempts",
            name=op.f("ck_processing_jobs_attempt_range"),
        ),
        sa.CheckConstraint(
            "max_attempts >= 1", name=op.f("ck_processing_jobs_max_attempts_positive")
        ),
        sa.CheckConstraint(
            "progress >= 0 AND progress <= 1", name=op.f("ck_processing_jobs_progress_range")
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["profiles.id"],
            name=op.f("fk_processing_jobs_owner_id_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_processing_jobs_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_jobs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_processing_jobs_idempotency_key")),
    )
    op.create_index(
        "ix_processing_jobs_leases",
        "processing_jobs",
        ["lease_expires_at"],
        unique=False,
        postgresql_where=sa.text("state = 'running'"),
    )
    op.create_index(
        op.f("ix_processing_jobs_owner_id"), "processing_jobs", ["owner_id"], unique=False
    )
    op.create_index(
        "ix_processing_jobs_runnable",
        "processing_jobs",
        ["run_after"],
        unique=False,
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.create_index(
        op.f("ix_processing_jobs_video_id"), "processing_jobs", ["video_id"], unique=False
    )
    op.create_table(
        "uploads",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("video_id", sa.UUID(), nullable=False),
        sa.Column("storage_upload_id", sa.String(length=1024), nullable=False),
        sa.Column("part_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("part_count", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "part_count BETWEEN 1 AND 10000", name=op.f("ck_uploads_part_count_range")
        ),
        sa.CheckConstraint(
            "part_size_bytes BETWEEN 5242880 AND 5368709120",
            name=op.f("ck_uploads_part_size_range"),
        ),
        sa.ForeignKeyConstraint(
            ["video_id"], ["videos.id"], name=op.f("fk_uploads_video_id_videos"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_uploads")),
        sa.UniqueConstraint("video_id", name=op.f("uq_uploads_video_id")),
    )
    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("video_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("pipeline_version", sa.String(length=64), nullable=False),
        sa.Column(
            "model_version_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "failed",
                name="run_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="running",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "NOT is_active OR status = 'succeeded'", name=op.f("ck_analysis_runs_active_succeeded")
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["processing_jobs.id"],
            name=op.f("fk_analysis_runs_job_id_processing_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_analysis_runs_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_runs")),
    )
    op.create_index(op.f("ix_analysis_runs_job_id"), "analysis_runs", ["job_id"], unique=False)
    op.create_index(op.f("ix_analysis_runs_video_id"), "analysis_runs", ["video_id"], unique=False)
    op.create_index(
        "uq_analysis_runs_one_active",
        "analysis_runs",
        ["video_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "highlight_exports",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("video_id", sa.UUID(), nullable=False),
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("clips", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "options",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "ready",
                "failed",
                "expired",
                name="export_status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("output_key", sa.String(length=512), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "jsonb_typeof(clips) = 'array' AND jsonb_array_length(clips) > 0",
            name=op.f("ck_highlight_exports_clips_nonempty"),
        ),
        sa.CheckConstraint(
            "status <> 'ready' OR (output_key IS NOT NULL AND coalesce(size_bytes > 0, false))",
            name=op.f("ck_highlight_exports_ready_has_output"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["processing_jobs.id"],
            name=op.f("fk_highlight_exports_job_id_processing_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["profiles.id"],
            name=op.f("fk_highlight_exports_owner_id_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_highlight_exports_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_highlight_exports")),
        sa.UniqueConstraint("output_key", name=op.f("uq_highlight_exports_output_key")),
    )
    op.create_index(
        op.f("ix_highlight_exports_job_id"), "highlight_exports", ["job_id"], unique=False
    )
    op.create_index(
        op.f("ix_highlight_exports_owner_id"), "highlight_exports", ["owner_id"], unique=False
    )
    op.create_index(
        op.f("ix_highlight_exports_video_id"), "highlight_exports", ["video_id"], unique=False
    )
    op.create_table(
        "job_stage_metrics",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("wall_s", sa.Double(), nullable=True),
        sa.Column("cpu_s", sa.Double(), nullable=True),
        sa.Column("gpu_s", sa.Double(), nullable=True),
        sa.Column("peak_mem_mb", sa.Double(), nullable=True),
        sa.Column("frames_processed", sa.BigInteger(), nullable=True),
        sa.Column("worker_kind", sa.String(length=32), nullable=True),
        sa.Column("instance_type", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "coalesce(wall_s, 0) >= 0 AND coalesce(cpu_s, 0) >= 0 AND coalesce(gpu_s, 0) >= 0",
            name=op.f("ck_job_stage_metrics_nonneg"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_job_stage_metrics_time_order"),
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["processing_jobs.id"],
            name=op.f("fk_job_stage_metrics_job_id_processing_jobs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", "attempt", "stage", name=op.f("pk_job_stage_metrics")),
    )
    op.create_table(
        "camera_segments",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("analysis_run_id", sa.UUID(), nullable=False),
        sa.Column("start_t", sa.Double(), nullable=False),
        sa.Column("end_t", sa.Double(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "manual",
                "model",
                name="calibration_source",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
        sa.Column("image_keypoints", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("homography", postgresql.ARRAY(sa.Double()), nullable=True),
        sa.Column("reprojection_err_px", sa.Double(), nullable=True),
        sa.Column("confidence", sa.Double(), nullable=True),
        postgresql.ExcludeConstraint(
            (sa.column("analysis_run_id"), "="),
            (sa.text("numrange(start_t::numeric, end_t::numeric, '[)')"), "&&"),
            using="gist",
            name="ex_camera_segments_no_overlap",
        ),
        sa.CheckConstraint(
            "(source IS NULL) = (image_keypoints IS NULL)",
            name=op.f("ck_camera_segments_calibrated_has_source"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name=op.f("ck_camera_segments_confidence_range"),
        ),
        sa.CheckConstraint(
            "homography IS NULL OR cardinality(homography) = 9",
            name=op.f("ck_camera_segments_homography_3x3"),
        ),
        sa.CheckConstraint(
            "start_t >= 0 AND end_t > start_t", name=op.f("ck_camera_segments_time_order")
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_camera_segments_analysis_run_id_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_camera_segments")),
    )
    op.create_index(
        op.f("ix_camera_segments_analysis_run_id"),
        "camera_segments",
        ["analysis_run_id"],
        unique=False,
    )
    op.create_table(
        "rallies",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("analysis_run_id", sa.UUID(), nullable=False),
        sa.Column("video_id", sa.UUID(), nullable=False),
        sa.Column("start_t", sa.Double(), nullable=False),
        sa.Column("end_t", sa.Double(), nullable=False),
        sa.Column("serve_t", sa.Double(), nullable=True),
        sa.Column("confidence", sa.Double(), nullable=True),
        sa.Column("detector", sa.String(length=64), nullable=False),
        sa.Column("model_version_id", sa.UUID(), nullable=True),
        sa.Column(
            "features",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        postgresql.ExcludeConstraint(
            (sa.column("analysis_run_id"), "="),
            (sa.text("numrange(start_t::numeric, end_t::numeric, '[)')"), "&&"),
            using="gist",
            name="ex_rallies_no_overlap",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name=op.f("ck_rallies_confidence_range"),
        ),
        sa.CheckConstraint(
            "serve_t IS NULL OR (serve_t >= start_t AND serve_t <= end_t)",
            name=op.f("ck_rallies_serve_inside"),
        ),
        sa.CheckConstraint("start_t >= 0 AND end_t > start_t", name=op.f("ck_rallies_time_order")),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_runs.id"],
            name=op.f("fk_rallies_analysis_run_id_analysis_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.id"],
            name=op.f("fk_rallies_model_version_id_model_versions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"], ["videos.id"], name=op.f("fk_rallies_video_id_videos"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rallies")),
    )
    op.create_index(
        op.f("ix_rallies_analysis_run_id"), "rallies", ["analysis_run_id"], unique=False
    )
    op.create_index(
        op.f("ix_rallies_model_version_id"), "rallies", ["model_version_id"], unique=False
    )
    op.create_index("ix_rallies_run_start", "rallies", ["analysis_run_id", "start_t"], unique=False)
    op.create_index(op.f("ix_rallies_video_id"), "rallies", ["video_id"], unique=False)
    op.create_table(
        "rally_edits",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("video_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("base_rally_id", sa.UUID(), nullable=True),
        sa.Column("base_edit_id", sa.UUID(), nullable=True),
        sa.Column(
            "op",
            sa.Enum(
                "add",
                "delete",
                "modify",
                name="edit_op",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("start_t", sa.Double(), nullable=True),
        sa.Column("end_t", sa.Double(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("superseded_by", sa.UUID(), nullable=True),
        sa.CheckConstraint(
            "op <> 'add' OR (base_rally_id IS NULL AND base_edit_id IS NULL)",
            name=op.f("ck_rally_edits_add_has_no_target"),
        ),
        sa.CheckConstraint(
            "op <> 'delete' OR (start_t IS NULL AND end_t IS NULL)",
            name=op.f("ck_rally_edits_no_times_for_delete"),
        ),
        sa.CheckConstraint(
            "op = 'add' OR num_nonnulls(base_rally_id, base_edit_id) = 1",
            name=op.f("ck_rally_edits_target_for_modify_delete"),
        ),
        sa.CheckConstraint(
            "op = 'delete' OR (start_t IS NOT NULL AND end_t IS NOT NULL AND start_t >= 0 AND end_t > start_t)",
            name=op.f("ck_rally_edits_times_for_add_modify"),
        ),
        sa.ForeignKeyConstraint(
            ["base_edit_id"],
            ["rally_edits.id"],
            name=op.f("fk_rally_edits_base_edit_id_rally_edits"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["base_rally_id"],
            ["rallies.id"],
            name=op.f("fk_rally_edits_base_rally_id_rallies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["rally_edits.id"],
            name=op.f("fk_rally_edits_superseded_by_rally_edits"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["profiles.id"],
            name=op.f("fk_rally_edits_user_id_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_id"],
            ["videos.id"],
            name=op.f("fk_rally_edits_video_id_videos"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rally_edits")),
    )
    op.create_index(
        op.f("ix_rally_edits_base_edit_id"), "rally_edits", ["base_edit_id"], unique=False
    )
    op.create_index(
        op.f("ix_rally_edits_base_rally_id"), "rally_edits", ["base_rally_id"], unique=False
    )
    op.create_index(op.f("ix_rally_edits_user_id"), "rally_edits", ["user_id"], unique=False)
    op.create_index(op.f("ix_rally_edits_video_id"), "rally_edits", ["video_id"], unique=False)

    # Defence in depth: the browser never talks to Postgres directly (all access goes through
    # the API, which connects as the table owner and therefore bypasses RLS). Enabling RLS with
    # no policies denies everything to Supabase's "anon" and "authenticated" roles.
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(
        """
        DO $$
        DECLARE r text;
        BEGIN
          FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
              EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', r);
            END IF;
          END LOOP;
        END $$;
        """
    )


def downgrade() -> None:
    # The btree_gist extension is left installed; other schemas may use it.
    op.drop_index(op.f("ix_rally_edits_video_id"), table_name="rally_edits")
    op.drop_index(op.f("ix_rally_edits_user_id"), table_name="rally_edits")
    op.drop_index(op.f("ix_rally_edits_base_rally_id"), table_name="rally_edits")
    op.drop_index(op.f("ix_rally_edits_base_edit_id"), table_name="rally_edits")
    op.drop_table("rally_edits")
    op.drop_index(op.f("ix_rallies_video_id"), table_name="rallies")
    op.drop_index("ix_rallies_run_start", table_name="rallies")
    op.drop_index(op.f("ix_rallies_model_version_id"), table_name="rallies")
    op.drop_index(op.f("ix_rallies_analysis_run_id"), table_name="rallies")
    op.drop_table("rallies")
    op.drop_index(op.f("ix_camera_segments_analysis_run_id"), table_name="camera_segments")
    op.drop_table("camera_segments")
    op.drop_table("job_stage_metrics")
    op.drop_index(op.f("ix_highlight_exports_video_id"), table_name="highlight_exports")
    op.drop_index(op.f("ix_highlight_exports_owner_id"), table_name="highlight_exports")
    op.drop_index(op.f("ix_highlight_exports_job_id"), table_name="highlight_exports")
    op.drop_table("highlight_exports")
    op.drop_index(
        "uq_analysis_runs_one_active",
        table_name="analysis_runs",
        postgresql_where=sa.text("is_active"),
    )
    op.drop_index(op.f("ix_analysis_runs_video_id"), table_name="analysis_runs")
    op.drop_index(op.f("ix_analysis_runs_job_id"), table_name="analysis_runs")
    op.drop_table("analysis_runs")
    op.drop_table("uploads")
    op.drop_index(op.f("ix_processing_jobs_video_id"), table_name="processing_jobs")
    op.drop_index(
        "ix_processing_jobs_runnable",
        table_name="processing_jobs",
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.drop_index(op.f("ix_processing_jobs_owner_id"), table_name="processing_jobs")
    op.drop_index(
        "ix_processing_jobs_leases",
        table_name="processing_jobs",
        postgresql_where=sa.text("state = 'running'"),
    )
    op.drop_table("processing_jobs")
    op.drop_index(op.f("ix_videos_owner_id"), table_name="videos")
    op.drop_index("ix_videos_owner_created", table_name="videos")
    op.drop_table("videos")
    op.drop_table("profiles")
    op.drop_index(
        "uq_model_versions_one_production",
        table_name="model_versions",
        postgresql_where=sa.text("status = 'production'"),
    )
    op.drop_table("model_versions")
