"""Ingest end to end: Postgres + S3 (SeaweedFS) + ffmpeg, driven by the real worker loop."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from mediakit import make_clip
from pydantic import SecretStr
from sqlalchemy import Engine, text

from vball_core import jobs, pipelines
from vball_core.db.enums import JobKind
from vball_core.settings import Environment, Settings
from vball_core.storage import ObjectStorage
from vball_worker.media import probe
from vball_worker.runner import Worker

pytestmark = [pytest.mark.db, pytest.mark.ffmpeg]


@pytest.fixture
def settings(migrated_url: str, s3_env: dict[str, str], tmp_path: Path) -> Settings:
    return Settings(
        env=Environment.TEST,
        database_url=SecretStr(migrated_url),
        log_json=False,
        s3_endpoint_url=s3_env["endpoint"],
        s3_access_key_id=SecretStr(s3_env["key"]),
        s3_secret_access_key=SecretStr(s3_env["secret"]),
        s3_bucket=s3_env["bucket"],
        worker_scratch_dir=str(tmp_path),
    )


@pytest.fixture
def storage(settings: Settings) -> ObjectStorage:
    s = ObjectStorage(settings)
    s.ensure_bucket()
    return s


@pytest.fixture
def worker(settings: Settings, engine: Engine, storage: ObjectStorage) -> Worker:
    return Worker(settings, engine=engine, storage=storage, worker_id="test-worker")


def uploaded_video(
    engine: Engine, storage: ObjectStorage, file: Path | None, suffix: str = ".mov"
) -> dict[str, Any]:
    """Rows + object as they are right after POST /videos/{id}/upload/complete."""
    owner, video_id = uuid.uuid4(), uuid.uuid4()
    key = f"originals/{owner}/{video_id}{suffix}"
    size = file.stat().st_size if file else 1234
    if file:
        storage.upload(str(file), key, "video/quicktime")
    with engine.begin() as c:
        c.execute(text("INSERT INTO profiles (id) VALUES (:id)"), {"id": owner})
        c.execute(
            text(
                "INSERT INTO videos (id, owner_id, filename, size_bytes, content_type, "
                "original_key, status) VALUES (:id, :o, 'match.mov', :s, 'video/quicktime', :k, "
                "'uploaded')"
            ),
            {"id": video_id, "o": owner, "s": size, "k": key},
        )
        jobs.enqueue(
            c,
            video_id=video_id,
            owner_id=owner,
            kind=JobKind.INGEST,
            pipeline_version=pipelines.INGEST,
        )
    return {"id": video_id, "owner": owner, "key": key}


def video_row(engine: Engine, video_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as c:
        return dict(
            c.execute(text("SELECT * FROM videos WHERE id = :id"), {"id": video_id})
            .mappings()
            .one()
        )


def job_row(engine: Engine, video_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as c:
        return dict(
            c.execute(text("SELECT * FROM processing_jobs WHERE video_id = :id"), {"id": video_id})
            .mappings()
            .one()
        )


def test_ingest_makes_proxy_and_deletes_original(
    worker: Worker, engine: Engine, storage: ObjectStorage, tmp_path: Path
) -> None:
    clip = make_clip(tmp_path / "match.mov", seconds=4, size="1920x1080")
    v = uploaded_video(engine, storage, clip)

    assert worker.run_until_idle() >= 1

    row = video_row(engine, v["id"])
    assert row["status"] == "ready"
    assert (row["width"], row["height"]) == (1920, 1080)
    assert 3.9 < row["duration_s"] < 4.2
    assert row["fps_num"] / row["fps_den"] == 30
    assert row["video_codec"] == "h264"
    assert row["sha256"]
    assert len(row["sha256"]) == 64
    assert row["proxy_size_bytes"] > 0
    assert row["original_deleted_at"] is not None
    assert storage.object_size(v["key"]) is None  # original gone (free-tier budget)

    proxy = tmp_path / "proxy.mp4"
    storage.download(row["proxy_key"], str(proxy))
    p = probe(proxy)
    assert (p.width, p.height) == (1280, 720)
    assert storage.object_size(row["poster_key"])

    job = job_row(engine, v["id"])
    assert job["state"] == "succeeded"
    assert job["progress"] == 1.0
    with engine.connect() as c:
        stages = {
            r[0]
            for r in c.execute(
                text("SELECT stage FROM job_stage_metrics WHERE job_id = :id"), {"id": job["id"]}
            )
        }
    assert {"download", "probe", "transcode", "poster", "upload"} <= stages


def test_keep_originals_setting(
    settings: Settings, engine: Engine, storage: ObjectStorage, tmp_path: Path
) -> None:
    keep = settings.model_copy(update={"keep_originals": True})
    w = Worker(keep, engine=engine, storage=storage, worker_id="keeper")
    v = uploaded_video(engine, storage, make_clip(tmp_path / "a.mov", seconds=2))
    w.run_until_idle()
    assert video_row(engine, v["id"])["original_deleted_at"] is None
    assert storage.object_size(v["key"])


def test_not_a_video_fails_without_retry(
    worker: Worker, engine: Engine, storage: ObjectStorage, tmp_path: Path
) -> None:
    junk = tmp_path / "junk.mov"
    junk.write_bytes(b"definitely not a video " * 500)
    v = uploaded_video(engine, storage, junk)
    worker.run_until_idle()
    job = job_row(engine, v["id"])
    assert job["state"] == "failed"
    assert job["error_code"] == "input_invalid"
    assert job["attempt"] == 1
    row = video_row(engine, v["id"])
    assert row["status"] == "failed"
    assert "couldn't be read as a video" in row["error_message"]
    assert storage.object_size(v["key"])  # original kept so the user can see what happened


def test_missing_original_is_retried_then_gives_up(
    worker: Worker, engine: Engine, storage: ObjectStorage
) -> None:
    v = uploaded_video(engine, storage, None)  # row exists, object doesn't
    worker.run_until_idle()
    job = job_row(engine, v["id"])
    assert job["state"] == "queued"  # transient storage error -> retry with backoff
    assert job["error_code"] == "transient"
    assert video_row(engine, v["id"])["status"] == "ingesting"

    for _ in range(5):  # skip the backoff and let attempts run out
        with engine.begin() as c:
            c.execute(
                text("UPDATE processing_jobs SET run_after = now() WHERE video_id = :id"),
                {"id": v["id"]},
            )
        worker.run_until_idle()
    job = job_row(engine, v["id"])
    assert job["state"] == "failed"
    assert job["attempt"] == job["max_attempts"]
    assert video_row(engine, v["id"])["status"] == "failed"


def test_cancelled_job_is_not_run(
    worker: Worker, engine: Engine, storage: ObjectStorage, tmp_path: Path
) -> None:
    v = uploaded_video(engine, storage, make_clip(tmp_path / "c.mov", seconds=2))
    with engine.begin() as c:
        c.execute(
            text("UPDATE processing_jobs SET cancel_requested = true WHERE video_id = :id"),
            {"id": v["id"]},
        )
    worker.run_until_idle()
    assert job_row(engine, v["id"])["state"] == "cancelled"
    assert video_row(engine, v["id"])["status"] == "uploaded"


def test_lost_lease_during_processing_writes_nothing(
    settings: Settings, engine: Engine, storage: ObjectStorage, tmp_path: Path
) -> None:
    """Heartbeat finds the job cancelled mid-transcode: the worker abandons it."""
    v = uploaded_video(engine, storage, make_clip(tmp_path / "d.mov", seconds=6))
    w = Worker(settings, engine=engine, storage=storage, worker_id="w", heartbeat_interval_s=0.2)

    from vball_worker import runner

    original = runner.HANDLERS[JobKind.INGEST]

    def cancel_then_run(ctx: Any, s: Settings, st: ObjectStorage) -> Any:
        with engine.begin() as c:
            c.execute(
                text("UPDATE processing_jobs SET cancel_requested = true WHERE id = :id"),
                {"id": ctx.job.id},
            )
        return original(ctx, s, st)

    runner.HANDLERS[JobKind.INGEST] = cancel_then_run
    try:
        w.run_until_idle()
    finally:
        runner.HANDLERS[JobKind.INGEST] = original
    row = video_row(engine, v["id"])
    assert row["status"] == "ingesting"  # not marked ready
    assert row["proxy_key"] is None
    assert job_row(engine, v["id"])["state"] == "running"  # left for sweep to finalise
