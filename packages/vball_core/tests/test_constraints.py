"""Database constraints reject invalid data. Each case runs inside a savepoint."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = pytest.mark.db

SHA = "a" * 64


def insert(conn: Connection, table: str, **values: Any) -> uuid.UUID | None:
    cols = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    row = conn.execute(
        text(f"INSERT INTO {table} ({cols}) VALUES ({params}) RETURNING *"), values
    ).first()
    return getattr(row, "id", None) if row else None


def rejected(conn: Connection, table: str, **values: Any) -> None:
    # The savepoint must be inside pytest.raises so it rolls back before the error is caught.
    with pytest.raises((IntegrityError, DBAPIError)), conn.begin_nested():
        insert(conn, table, **values)


def accepted(conn: Connection, table: str, **values: Any) -> uuid.UUID | None:
    with conn.begin_nested():
        return insert(conn, table, **values)


@pytest.fixture
def world(conn: Connection) -> dict[str, uuid.UUID]:
    owner = uuid.uuid4()
    insert(conn, "profiles", id=owner)
    video = insert(
        conn,
        "videos",
        owner_id=owner,
        filename="match.mp4",
        size_bytes=1000,
        content_type="video/mp4",
        original_key=f"originals/{owner}/{uuid.uuid4()}.mp4",
    )
    run = insert(conn, "analysis_runs", video_id=video, pipeline_version="p1")
    assert video
    assert run
    return {"owner": owner, "video": video, "run": run}


def video_values(world: dict[str, uuid.UUID], **over: Any) -> dict[str, Any]:
    return {
        "owner_id": world["owner"],
        "filename": "x.mp4",
        "size_bytes": 10,
        "content_type": "video/mp4",
        "original_key": f"originals/{uuid.uuid4()}",
        **over,
    }


def job_values(world: dict[str, uuid.UUID], **over: Any) -> dict[str, Any]:
    return {
        "video_id": world["video"],
        "owner_id": world["owner"],
        "kind": "ingest",
        "idempotency_key": uuid.uuid4().hex,
        "pipeline_version": "p1",
        **over,
    }


def rally_values(world: dict[str, uuid.UUID], **over: Any) -> dict[str, Any]:
    return {
        "analysis_run_id": world["run"],
        "video_id": world["video"],
        "start_t": 10.0,
        "end_t": 20.0,
        "detector": "heuristic-v1",
        **over,
    }


class TestVideos:
    def test_valid_video_accepted(self, conn: Connection, world: dict[str, uuid.UUID]) -> None:
        accepted(conn, "videos", **video_values(world))

    @pytest.mark.parametrize(
        "over",
        [
            {"size_bytes": 0},
            {"status": "bogus"},
            {"status": "ready"},  # ready requires duration + proxy
            {"status": "deleted"},  # deleted requires deleted_at
            {"duration_s": 0},
            {"fps_num": 30},  # fps_den missing
            {"width": 1920},  # height missing
            {"fps_num": 0, "fps_den": 1},
            {"width": 0, "height": 1080},
            {"rotation": 45},
            {"sha256": "XYZ"},
        ],
    )
    def test_invalid_video_rejected(
        self, conn: Connection, world: dict[str, uuid.UUID], over: dict[str, Any]
    ) -> None:
        rejected(conn, "videos", **video_values(world, **over))

    def test_unknown_owner_rejected(self, conn: Connection, world: dict[str, uuid.UUID]) -> None:
        rejected(conn, "videos", **video_values(world, owner_id=uuid.uuid4()))

    def test_ready_with_media_accepted(self, conn: Connection, world: dict[str, uuid.UUID]) -> None:
        accepted(
            conn,
            "videos",
            **video_values(world, status="ready", duration_s=60.0, proxy_key="p/1.mp4"),
        )


class TestUploads:
    def test_part_limits(self, conn: Connection, world: dict[str, uuid.UUID]) -> None:
        base = {"storage_upload_id": "u", "expires_at": "2030-01-01T00:00:00Z"}
        rejected(
            conn, "uploads", video_id=world["video"], part_size_bytes=1024, part_count=1, **base
        )
        rejected(
            conn,
            "uploads",
            video_id=world["video"],
            part_size_bytes=8 << 20,
            part_count=10001,
            **base,
        )
        accepted(
            conn, "uploads", video_id=world["video"], part_size_bytes=64 << 20, part_count=3, **base
        )
        # one upload per video
        rejected(
            conn, "uploads", video_id=world["video"], part_size_bytes=64 << 20, part_count=3, **base
        )


class TestJobs:
    def test_queued_job_accepted(self, conn: Connection, world: dict[str, uuid.UUID]) -> None:
        accepted(conn, "processing_jobs", **job_values(world))

    def test_idempotency_key_unique(self, conn: Connection, world: dict[str, uuid.UUID]) -> None:
        accepted(conn, "processing_jobs", **job_values(world, idempotency_key="k"))
        rejected(conn, "processing_jobs", **job_values(world, idempotency_key="k"))

    @pytest.mark.parametrize(
        "over",
        [
            {"state": "running"},  # no lease
            {"state": "succeeded", "finished_at": "2030-01-01T00:00:00Z", "progress": 0.5},
            {"state": "succeeded", "progress": 1},  # no finished_at
            {"state": "failed", "finished_at": "2030-01-01T00:00:00Z"},  # no error code
            {"finished_at": "2030-01-01T00:00:00Z"},  # queued but finished
            {"progress": 1.5},
            {"attempt": 4, "max_attempts": 3},
            {"max_attempts": 0},
            {"kind": "train_model"},
        ],
    )
    def test_invalid_job_rejected(
        self, conn: Connection, world: dict[str, uuid.UUID], over: dict[str, Any]
    ) -> None:
        rejected(conn, "processing_jobs", **job_values(world, **over))

    def test_valid_terminal_states(self, conn: Connection, world: dict[str, uuid.UUID]) -> None:
        done = "2030-01-01T00:00:00Z"
        accepted(
            conn,
            "processing_jobs",
            **job_values(world, state="succeeded", progress=1, finished_at=done),
        )
        accepted(
            conn,
            "processing_jobs",
            **job_values(world, state="failed", error_code="input_invalid", finished_at=done),
        )
        accepted(
            conn,
            "processing_jobs",
            **job_values(
                world, state="running", lease_owner="w1", lease_expires_at="2030-01-01T00:05:00Z"
            ),
        )


class TestRallies:
    def test_valid_rally_accepted(self, conn: Connection, world: dict[str, uuid.UUID]) -> None:
        accepted(conn, "rallies", **rally_values(world, serve_t=12.0, confidence=0.9))

    @pytest.mark.parametrize(
        "over",
        [
            {"end_t": 10.0},  # zero length
            {"end_t": 5.0},  # reversed
            {"start_t": -1.0},
            {"serve_t": 25.0},  # outside rally
            {"confidence": 1.2},
        ],
    )
    def test_invalid_rally_rejected(
        self, conn: Connection, world: dict[str, uuid.UUID], over: dict[str, Any]
    ) -> None:
        rejected(conn, "rallies", **rally_values(world, **over))

    def test_no_overlap_within_run(self, conn: Connection, world: dict[str, uuid.UUID]) -> None:
        accepted(conn, "rallies", **rally_values(world, start_t=10.0, end_t=20.0))
        rejected(conn, "rallies", **rally_values(world, start_t=19.0, end_t=30.0))
        # touching boundaries are allowed ([) ranges)
        accepted(conn, "rallies", **rally_values(world, start_t=20.0, end_t=30.0))

    def test_overlap_allowed_across_runs(
        self, conn: Connection, world: dict[str, uuid.UUID]
    ) -> None:
        other_run = insert(conn, "analysis_runs", video_id=world["video"], pipeline_version="p2")
        accepted(conn, "rallies", **rally_values(world))
        accepted(conn, "rallies", **rally_values(world, analysis_run_id=other_run))


class TestRunsAndModels:
    def test_one_active_run_per_video(self, conn: Connection, world: dict[str, uuid.UUID]) -> None:
        rejected(
            conn, "analysis_runs", video_id=world["video"], pipeline_version="p", is_active=True
        )
        accepted(
            conn,
            "analysis_runs",
            video_id=world["video"],
            pipeline_version="p",
            status="succeeded",
            is_active=True,
        )
        rejected(
            conn,
            "analysis_runs",
            video_id=world["video"],
            pipeline_version="p",
            status="succeeded",
            is_active=True,
        )

    def test_one_production_model_per_task(self, conn: Connection) -> None:
        base = {"task": "rally", "name": "tcn", "artifact_uri": "r2://m", "framework": "onnx"}
        prod = {"status": "production", "promoted_at": "2030-01-01T00:00:00Z"}
        rejected(conn, "model_versions", version="0", sha256="nothex", **base)
        rejected(conn, "model_versions", version="0", sha256=SHA, status="production", **base)
        accepted(conn, "model_versions", version="1", sha256=SHA, **prod, **base)
        rejected(conn, "model_versions", version="2", sha256=SHA, **prod, **base)
        rejected(conn, "model_versions", version="1", sha256=SHA, **base)  # duplicate version
        accepted(conn, "model_versions", version="2", sha256=SHA, **base)  # candidate is fine


class TestCameraSegments:
    def test_segments(self, conn: Connection, world: dict[str, uuid.UUID]) -> None:
        base = {"analysis_run_id": world["run"]}
        accepted(conn, "camera_segments", start_t=0.0, end_t=100.0, **base)
        rejected(conn, "camera_segments", start_t=50.0, end_t=150.0, **base)
        rejected(
            conn,
            "camera_segments",
            start_t=100.0,
            end_t=200.0,
            homography=[1.0, 0.0, 0.0],
            **base,
        )
        rejected(conn, "camera_segments", start_t=100.0, end_t=200.0, source="manual", **base)


class TestRallyEdits:
    def test_edit_shapes(self, conn: Connection, world: dict[str, uuid.UUID]) -> None:
        rally = insert(conn, "rallies", **rally_values(world))
        base = {"video_id": world["video"], "user_id": world["owner"]}
        accepted(conn, "rally_edits", op="add", start_t=30.0, end_t=40.0, **base)
        accepted(
            conn, "rally_edits", op="modify", base_rally_id=rally, start_t=9.0, end_t=21.0, **base
        )
        accepted(conn, "rally_edits", op="delete", base_rally_id=rally, **base)
        rejected(conn, "rally_edits", op="add", start_t=30.0, **base)  # no end
        rejected(conn, "rally_edits", op="add", base_rally_id=rally, start_t=1.0, end_t=2.0, **base)
        rejected(conn, "rally_edits", op="modify", start_t=1.0, end_t=2.0, **base)  # no target
        rejected(
            conn, "rally_edits", op="delete", base_rally_id=rally, start_t=1.0, end_t=2.0, **base
        )
        rejected(
            conn, "rally_edits", op="modify", base_rally_id=rally, start_t=5.0, end_t=4.0, **base
        )


class TestHighlightExports:
    def test_exports(self, conn: Connection, world: dict[str, uuid.UUID]) -> None:
        base = {"video_id": world["video"], "owner_id": world["owner"]}
        rejected(conn, "highlight_exports", clips="[]", **base)
        rejected(conn, "highlight_exports", clips='{"a": 1}', **base)
        rejected(conn, "highlight_exports", clips="[[1, 2]]", status="ready", **base)
        rejected(
            conn, "highlight_exports", clips="[[1, 2]]", status="ready", output_key="h/0", **base
        )  # size missing
        accepted(conn, "highlight_exports", clips="[[1, 2]]", **base)
        accepted(
            conn,
            "highlight_exports",
            clips="[[1, 2]]",
            status="ready",
            output_key="h/1.mp4",
            size_bytes=5,
            **base,
        )


def test_deleting_profile_cascades(conn: Connection, world: dict[str, uuid.UUID]) -> None:
    insert(conn, "rallies", **rally_values(world))
    conn.execute(text("DELETE FROM profiles WHERE id = :id"), {"id": world["owner"]})
    for table in ("videos", "analysis_runs", "rallies"):
        assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
