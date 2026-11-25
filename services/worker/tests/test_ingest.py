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
            c.execute(
                text("SELECT * FROM processing_jobs WHERE video_id = :id AND kind = 'ingest'"),
                {"id": video_id},
            )
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


def test_analysis_runs_after_ingest(
    worker: Worker, engine: Engine, storage: ObjectStorage, tmp_path: Path
) -> None:
    v = uploaded_video(engine, storage, make_clip(tmp_path / "m.mov", seconds=6))
    assert worker.run_until_idle() == 2  # ingest, then analyze
    with engine.connect() as c:
        run = (
            c.execute(
                text("SELECT * FROM analysis_runs WHERE video_id = :id AND is_active"),
                {"id": v["id"]},
            )
            .mappings()
            .one()
        )
        segs = c.execute(
            text("SELECT count(*) FROM camera_segments WHERE analysis_run_id = :r"),
            {"r": run["id"]},
        ).scalar_one()
    assert run["status"] == "succeeded"
    assert run["pipeline_version"] == "rally-heuristic-1"
    assert run["params"]["detector"] == "heuristic-v1"
    assert segs >= 1
    assert storage.object_size(run["params"]["features_key"])


def _register_model(
    engine: Engine, storage: ObjectStorage, tmp_path: Path, corrupt: bool = False
) -> str:
    import hashlib

    import numpy as np

    from vball_ml.rally_model import Bundle, labels_for, save_bundle, train

    x = np.random.default_rng(0).normal(size=(400, 416)).astype(np.float32)
    y = labels_for(400, [(10.0, 30.0), (50.0, 70.0)])
    x[y == 1, 0] += 3.0  # learnable signal
    net, norm = train(x, y, np.ones(400, bool), epochs=40, crop=200)
    path = tmp_path / "model.pt"
    save_bundle(str(path), Bundle([net], norm, {"version": "test-1"}))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    key = f"models/rally/test-{uuid.uuid4()}/model.pt"
    storage.upload(str(path), key, "application/octet-stream")
    with engine.begin() as c:
        c.execute(text("UPDATE model_versions SET status='retired' WHERE status='production'"))
        c.execute(
            text(
                "INSERT INTO model_versions (task, name, version, artifact_uri, sha256, framework, "
                "status, promoted_at) VALUES ('rally','t',:v,:k,:s,'pytorch','production',now())"
            ),
            {"v": f"test-{uuid.uuid4().hex[:6]}", "k": key, "s": "0" * 64 if corrupt else digest},
        )
    return digest


@pytest.fixture
def fake_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    import numpy as np

    import vball_ml.features as feats

    def fake_extract(video: Any, play_area: Any, cache_dir: Any, **_: Any) -> Any:
        n = int(probe(video).duration_s / 0.2)
        x = np.zeros((n, 416), np.float32)
        x[: n // 2, 0] = 3.0  # first half looks like "rally" to the test model
        return x

    monkeypatch.setattr(feats, "extract", fake_extract)


def _active_run(engine: Engine, video_id: uuid.UUID) -> dict[str, Any]:
    with engine.connect() as c:
        return dict(
            c.execute(
                text("SELECT * FROM analysis_runs WHERE video_id=:v AND is_active"), {"v": video_id}
            )
            .mappings()
            .one()
        )


def test_learned_model_used_when_registered(
    worker: Worker, engine: Engine, storage: ObjectStorage, tmp_path: Path, fake_embeddings: None
) -> None:
    _register_model(engine, storage, tmp_path)
    try:
        v = uploaded_video(engine, storage, make_clip(tmp_path / "l.mov", seconds=8))
        worker.run_until_idle()
        run = _active_run(engine, v["id"])
        assert run["pipeline_version"] == "rally-learned-1"
        assert run["params"]["detector"].startswith("learned:")
        assert run["model_version_ids"]
        with engine.connect() as c:
            rows = c.execute(
                text("SELECT detector, confidence FROM rallies WHERE analysis_run_id=:r"),
                {"r": run["id"]},
            ).all()
        assert rows
        assert all(r.detector.startswith("learned:") and 0 <= r.confidence <= 1 for r in rows)
    finally:
        with engine.begin() as c:
            c.execute(text("UPDATE model_versions SET status='retired' WHERE status='production'"))


def test_corrupt_model_falls_back_to_heuristic(
    worker: Worker, engine: Engine, storage: ObjectStorage, tmp_path: Path, fake_embeddings: None
) -> None:
    _register_model(engine, storage, tmp_path, corrupt=True)
    try:
        v = uploaded_video(engine, storage, make_clip(tmp_path / "c2.mov", seconds=6))
        worker.run_until_idle()
        run = _active_run(engine, v["id"])
        assert run["pipeline_version"] == "rally-heuristic-1"
        assert run["params"]["fallback_reason"].startswith("learned model failed")
    finally:
        with engine.begin() as c:
            c.execute(text("UPDATE model_versions SET status='retired' WHERE status='production'"))
