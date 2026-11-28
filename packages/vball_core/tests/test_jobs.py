"""Job queue behaviour against real Postgres."""

from __future__ import annotations

import threading
import uuid
from collections import Counter
from datetime import timedelta

import pytest
from sqlalchemy import Connection, Engine, text

from vball_core import jobs
from vball_core.db.enums import JobErrorCode, JobKind, JobState

pytestmark = pytest.mark.db


@pytest.fixture
def clean(engine: Engine) -> Engine:
    with engine.begin() as c:
        c.execute(text("TRUNCATE processing_jobs, videos, profiles CASCADE"))
    return engine


def make_video(conn: Connection) -> tuple[uuid.UUID, uuid.UUID]:
    owner = uuid.uuid4()
    conn.execute(text("INSERT INTO profiles (id) VALUES (:id)"), {"id": owner})
    video = conn.execute(
        text(
            "INSERT INTO videos (owner_id, filename, size_bytes, content_type, original_key) "
            "VALUES (:o, 'm.mp4', 10, 'video/mp4', :k) RETURNING id"
        ),
        {"o": owner, "k": f"originals/{uuid.uuid4()}"},
    ).scalar_one()
    return owner, video


def job_row(conn: Connection, job_id: uuid.UUID) -> dict[str, object]:
    return dict(
        conn.execute(text("SELECT * FROM processing_jobs WHERE id = :id"), {"id": job_id})
        .mappings()
        .one()
    )


def expire_lease(conn: Connection, job_id: uuid.UUID) -> None:
    conn.execute(
        text(
            "UPDATE processing_jobs SET lease_expires_at = now() - interval '1 second' "
            "WHERE id = :id"
        ),
        {"id": job_id},
    )


def enqueue_one(conn: Connection, max_attempts: int = 3) -> uuid.UUID:
    owner, video = make_video(conn)
    return jobs.enqueue(
        conn,
        video_id=video,
        owner_id=owner,
        kind=JobKind.INGEST,
        pipeline_version="p1",
        max_attempts=max_attempts,
    )


def test_enqueue_is_idempotent(clean: Engine) -> None:
    with clean.begin() as c:
        owner, video = make_video(c)
        args = {
            "video_id": video,
            "owner_id": owner,
            "kind": JobKind.INGEST,
            "pipeline_version": "p1",
        }
        a = jobs.enqueue(c, **args, params={"x": 1})
        b = jobs.enqueue(c, **args, params={"x": 1})
        other = jobs.enqueue(c, **args, params={"x": 2})
        assert a == b
        assert other != a


def test_claim_succeed_lifecycle(clean: Engine) -> None:
    with clean.begin() as c:
        job_id = enqueue_one(c)
    with clean.begin() as c:
        job = jobs.claim(c, "w1")
        assert job is not None
        assert job.id == job_id
        assert job.attempt == 1
        assert jobs.claim(c, "w2") is None  # nothing else runnable
        jobs.heartbeat(c, job_id, "w1", progress=0.5, stage="transcode")
        row = job_row(c, job_id)
        assert row["stage"] == "transcode"
        assert row["progress"] == 0.5
        jobs.succeed(c, job_id, "w1")
        row = job_row(c, job_id)
        assert row["state"] == "succeeded"
        assert row["progress"] == 1.0
        assert row["finished_at"] is not None


def test_heartbeat_never_reports_complete(clean: Engine) -> None:
    with clean.begin() as c:
        job_id = enqueue_one(c)
        jobs.claim(c, "w1")
        jobs.heartbeat(c, job_id, "w1", progress=1.0)
        assert job_row(c, job_id)["progress"] == 0.99


def test_concurrent_claims_take_each_job_exactly_once(clean: Engine) -> None:
    n_jobs, n_workers = 30, 8
    with clean.begin() as c:
        ids = {enqueue_one(c) for _ in range(n_jobs)}
    taken: list[uuid.UUID] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n_workers)

    def worker(name: str) -> None:
        barrier.wait()
        while True:
            with clean.begin() as c:
                job = jobs.claim(c, name)
                if job is None:
                    return
                jobs.succeed(c, job.id, name)
            with lock:
                taken.append(job.id)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert Counter(taken) == Counter(ids)  # every job exactly once


def test_lost_lease_cannot_write_results(clean: Engine) -> None:
    with clean.begin() as c:
        job_id = enqueue_one(c)
        jobs.claim(c, "slow")
        expire_lease(c, job_id)
        assert jobs.sweep(c) == (1, 0)
        assert jobs.claim(c, "fast") is not None
    with clean.begin() as c:
        with pytest.raises(jobs.LeaseLostError):
            jobs.heartbeat(c, job_id, "slow")
        with pytest.raises(jobs.LeaseLostError):
            jobs.succeed(c, job_id, "slow")
        with pytest.raises(jobs.LeaseLostError):
            jobs.fail(c, job_id, "slow", JobErrorCode.INTERNAL, "x", retryable=False)
        jobs.succeed(c, job_id, "fast")
        assert job_row(c, job_id)["attempt"] == 2


def test_sweep_fails_job_after_max_attempts(clean: Engine) -> None:
    with clean.begin() as c:
        job_id = enqueue_one(c, max_attempts=2)
        for _ in range(2):
            assert jobs.claim(c, "w") is not None
            expire_lease(c, job_id)
            jobs.sweep(c)
        row = job_row(c, job_id)
        assert row["state"] == "failed"
        assert row["error_code"] == "internal"
        assert jobs.claim(c, "w") is None


def test_retryable_failure_backs_off_then_fails(clean: Engine) -> None:
    with clean.begin() as c:
        job_id = enqueue_one(c, max_attempts=2)
        jobs.claim(c, "w")
        state = jobs.fail(c, job_id, "w", JobErrorCode.TRANSIENT, "network blip", retryable=True)
        assert state is JobState.QUEUED
        row = job_row(c, job_id)
        assert row["error_message"] == "network blip"
        assert jobs.claim(c, "w") is None  # backoff: not runnable yet
        c.execute(
            text("UPDATE processing_jobs SET run_after = now() WHERE id = :id"), {"id": job_id}
        )
        assert jobs.claim(c, "w") is not None
        assert (
            jobs.fail(c, job_id, "w", JobErrorCode.TRANSIENT, "again", retryable=True)
            is JobState.FAILED
        )


def test_input_invalid_fails_immediately(clean: Engine) -> None:
    with clean.begin() as c:
        job_id = enqueue_one(c)
        jobs.claim(c, "w")
        assert (
            jobs.fail(c, job_id, "w", JobErrorCode.INPUT_INVALID, "not a video", retryable=False)
            is JobState.FAILED
        )


def test_success_after_retry_clears_error(clean: Engine) -> None:
    with clean.begin() as c:
        job_id = enqueue_one(c)
        jobs.claim(c, "w")
        jobs.fail(c, job_id, "w", JobErrorCode.TRANSIENT, "blip", retryable=True)
        c.execute(
            text("UPDATE processing_jobs SET run_after = now() WHERE id = :id"), {"id": job_id}
        )
        jobs.claim(c, "w")
        jobs.succeed(
            c, job_id, "w"
        )  # would violate ck_processing_jobs_succeeded_clean if not cleared
        assert job_row(c, job_id)["error_code"] is None


def test_cancel_stops_running_and_queued_jobs(clean: Engine) -> None:
    with clean.begin() as c:
        running = enqueue_one(c)
        jobs.claim(c, "w")
        queued = enqueue_one(c)
        c.execute(text("UPDATE processing_jobs SET cancel_requested = true"))
        with pytest.raises(jobs.LeaseLostError, match="cancelled"):
            jobs.heartbeat(c, running, "w")
        assert jobs.claim(c, "w") is None
        jobs.sweep(c)
        assert job_row(c, queued)["state"] == "cancelled"


def test_kind_filter(clean: Engine) -> None:
    with clean.begin() as c:
        enqueue_one(c)
        assert jobs.claim(c, "w", kinds=[JobKind.HIGHLIGHT]) is None
        assert jobs.claim(c, "w", kinds=[JobKind.INGEST]) is not None


def test_runnable_count(clean: Engine) -> None:
    with clean.begin() as c:
        enqueue_one(c)
        enqueue_one(c)
        assert jobs.runnable_count(c) == 2
        jobs.claim(c, "w", lease=timedelta(minutes=1))
        assert jobs.runnable_count(c) == 1


def test_defer_requeues_without_using_an_attempt(clean: Engine) -> None:
    from datetime import UTC, datetime

    with clean.begin() as c:
        job_id = enqueue_one(c, max_attempts=2)
        jobs.claim(c, "w")
        until = datetime.now(UTC) + timedelta(hours=1)
        jobs.defer(c, job_id, "w", until, JobErrorCode.TRANSIENT, "storage cap")
        row = job_row(c, job_id)
        assert row["state"] == "queued"
        assert row["attempt"] == 0
        assert row["error_message"] == "storage cap"
        assert jobs.claim(c, "w") is None  # waits until `until`
        with pytest.raises(jobs.LeaseLostError):
            jobs.defer(c, job_id, "w", until, JobErrorCode.TRANSIENT, "not held")
