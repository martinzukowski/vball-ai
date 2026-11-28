"""Durable job queue on Postgres.

* ``enqueue`` is idempotent per (video, kind, pipeline version, params).
* Workers ``claim`` with ``FOR UPDATE SKIP LOCKED`` and hold a time-limited lease, extended by
  ``heartbeat``. A crashed or preempted worker's lease expires and ``sweep`` puts the job back
  in the queue (or fails it once attempts are used up).
* ``succeed`` / ``fail`` only take effect for the worker that holds the lease, so a worker that
  lost its lease can never overwrite the result of the worker that took over.
* The database CHECK constraints (migration 0001) reject impossible states, e.g. a succeeded
  job with progress < 1 or a failed job without an error code.

All functions take a synchronous SQLAlchemy ``Connection`` and do not commit: the caller owns
the transaction (the API enqueues in the same transaction that marks an upload complete).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import Connection, Table, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from vball_core.db.enums import JobErrorCode, JobKind, JobState
from vball_core.db.models import ProcessingJob

JOBS = cast(Table, ProcessingJob.__table__)
DEFAULT_LEASE = timedelta(minutes=5)
MAX_BACKOFF = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: uuid.UUID
    video_id: uuid.UUID
    owner_id: uuid.UUID
    kind: JobKind
    params: dict[str, Any]
    pipeline_version: str
    attempt: int
    max_attempts: int


class LeaseLostError(RuntimeError):
    """The worker no longer holds the job (lease expired and someone else took it, or the job
    was cancelled). The worker must stop without writing results."""


def idempotency_key(
    video_id: uuid.UUID, kind: JobKind, pipeline_version: str, params: dict[str, Any]
) -> str:
    blob = json.dumps(
        [str(video_id), kind.value, pipeline_version, params], sort_keys=True, default=str
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def enqueue(
    conn: Connection,
    *,
    video_id: uuid.UUID,
    owner_id: uuid.UUID,
    kind: JobKind,
    pipeline_version: str,
    params: dict[str, Any] | None = None,
    max_attempts: int = 3,
) -> uuid.UUID:
    """Create the job unless an identical one exists; returns the job id either way."""
    params = params or {}
    key = idempotency_key(video_id, kind, pipeline_version, params)
    stmt = (
        insert(JOBS)
        .values(
            video_id=video_id,
            owner_id=owner_id,
            kind=kind.value,
            idempotency_key=key,
            params=params,
            pipeline_version=pipeline_version,
            max_attempts=max_attempts,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(JOBS.c.id)
    )
    job_id = conn.execute(stmt).scalar_one_or_none()
    if job_id is None:
        job_id = conn.execute(select(JOBS.c.id).where(JOBS.c.idempotency_key == key)).scalar_one()
    return cast(uuid.UUID, job_id)


def claim(
    conn: Connection,
    worker_id: str,
    kinds: list[JobKind] | None = None,
    lease: timedelta = DEFAULT_LEASE,
) -> ClaimedJob | None:
    """Take the oldest runnable job, or None. Increments the attempt counter."""
    conds = [
        JOBS.c.state == JobState.QUEUED.value,
        JOBS.c.run_after <= func.now(),
        JOBS.c.attempt < JOBS.c.max_attempts,
        JOBS.c.cancel_requested.is_(False),
    ]
    if kinds:
        conds.append(JOBS.c.kind.in_([k.value for k in kinds]))
    candidate = (
        select(JOBS.c.id)
        .where(and_(*conds))
        .order_by(JOBS.c.run_after, JOBS.c.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    row = conn.execute(
        update(JOBS)
        .where(JOBS.c.id == candidate)
        .values(
            state=JobState.RUNNING.value,
            attempt=JOBS.c.attempt + 1,
            lease_owner=worker_id,
            lease_expires_at=func.now() + lease,
            heartbeat_at=func.now(),
            started_at=func.coalesce(JOBS.c.started_at, func.now()),
            error_code=None,
            error_message=None,
        )
        .returning(
            JOBS.c.id,
            JOBS.c.video_id,
            JOBS.c.owner_id,
            JOBS.c.kind,
            JOBS.c.params,
            JOBS.c.pipeline_version,
            JOBS.c.attempt,
            JOBS.c.max_attempts,
        )
    ).first()
    if row is None:
        return None
    return ClaimedJob(
        id=row.id,
        video_id=row.video_id,
        owner_id=row.owner_id,
        kind=JobKind(row.kind),
        params=dict(row.params or {}),
        pipeline_version=row.pipeline_version,
        attempt=row.attempt,
        max_attempts=row.max_attempts,
    )


def _owned(job_id: uuid.UUID, worker_id: str) -> Any:
    return and_(
        JOBS.c.id == job_id,
        JOBS.c.state == JobState.RUNNING.value,
        JOBS.c.lease_owner == worker_id,
    )


def heartbeat(
    conn: Connection,
    job_id: uuid.UUID,
    worker_id: str,
    *,
    progress: float | None = None,
    stage: str | None = None,
    lease: timedelta = DEFAULT_LEASE,
) -> None:
    """Extend the lease and record progress. Raises LeaseLostError if the job is no longer
    ours or a cancel was requested."""
    values: dict[str, Any] = {
        "lease_expires_at": func.now() + lease,
        "heartbeat_at": func.now(),
    }
    if progress is not None:
        values["progress"] = max(0.0, min(0.99, progress))  # 1.0 only on success
    if stage is not None:
        values["stage"] = stage
    row = conn.execute(
        update(JOBS)
        .where(_owned(job_id, worker_id))
        .values(**values)
        .returning(JOBS.c.cancel_requested)
    ).first()
    if row is None:
        raise LeaseLostError(f"job {job_id} is no longer held by {worker_id}")
    if row.cancel_requested:
        raise LeaseLostError(f"job {job_id} was cancelled")


def succeed(conn: Connection, job_id: uuid.UUID, worker_id: str) -> None:
    result = conn.execute(
        update(JOBS)
        .where(_owned(job_id, worker_id))
        .values(
            state=JobState.SUCCEEDED.value,
            progress=1.0,
            stage=None,
            finished_at=func.now(),
            lease_owner=None,
            lease_expires_at=None,
        )
    )
    if result.rowcount != 1:
        raise LeaseLostError(f"job {job_id} is no longer held by {worker_id}")


def fail(
    conn: Connection,
    job_id: uuid.UUID,
    worker_id: str,
    code: JobErrorCode,
    message: str,
    *,
    retryable: bool,
) -> JobState:
    """Record a failure. Retryable failures go back to the queue with exponential backoff
    until attempts run out. Returns the job's new state."""
    job = conn.execute(
        select(JOBS.c.attempt, JOBS.c.max_attempts).where(_owned(job_id, worker_id))
    ).first()
    if job is None:
        raise LeaseLostError(f"job {job_id} is no longer held by {worker_id}")
    message = message[:2000]
    if retryable and job.attempt < job.max_attempts:
        delay = min(MAX_BACKOFF, timedelta(seconds=30 * 2 ** (job.attempt - 1)))
        conn.execute(
            update(JOBS)
            .where(JOBS.c.id == job_id)
            .values(
                state=JobState.QUEUED.value,
                run_after=func.now() + delay,
                lease_owner=None,
                lease_expires_at=None,
                error_code=code.value,
                error_message=message,
            )
        )
        return JobState.QUEUED
    conn.execute(
        update(JOBS)
        .where(JOBS.c.id == job_id)
        .values(
            state=JobState.FAILED.value,
            finished_at=func.now(),
            lease_owner=None,
            lease_expires_at=None,
            error_code=code.value,
            error_message=message,
        )
    )
    return JobState.FAILED


def defer(
    conn: Connection,
    job_id: uuid.UUID,
    worker_id: str,
    until: datetime,
    code: JobErrorCode,
    message: str,
) -> None:
    """Put a job back in the queue until ``until`` without using up an attempt (the outside
    world said "not now", e.g. a storage provider's daily download cap)."""
    if conn.execute(select(JOBS.c.id).where(_owned(job_id, worker_id))).first() is None:
        raise LeaseLostError(f"job {job_id} is no longer held by {worker_id}")
    conn.execute(
        update(JOBS)
        .where(JOBS.c.id == job_id)
        .values(
            state=JobState.QUEUED.value,
            run_after=until,
            attempt=func.greatest(JOBS.c.attempt - 1, 0),
            lease_owner=None,
            lease_expires_at=None,
            error_code=code.value,
            error_message=message[:2000],
        )
    )


def sweep(conn: Connection) -> tuple[int, int]:
    """Recover jobs whose worker vanished: requeue if attempts remain, else fail.
    Also finalises cancellations of queued jobs. Returns (requeued, failed)."""
    expired = and_(
        JOBS.c.state == JobState.RUNNING.value,
        JOBS.c.lease_expires_at < func.now(),
    )
    requeued = conn.execute(
        update(JOBS)
        .where(expired, JOBS.c.attempt < JOBS.c.max_attempts, JOBS.c.cancel_requested.is_(False))
        .values(
            state=JobState.QUEUED.value,
            run_after=func.now(),
            lease_owner=None,
            lease_expires_at=None,
            error_code=JobErrorCode.TRANSIENT.value,
            error_message="worker stopped responding; retrying",
        )
    ).rowcount
    failed = conn.execute(
        update(JOBS)
        .where(expired, or_(JOBS.c.attempt >= JOBS.c.max_attempts, JOBS.c.cancel_requested))
        .values(
            state=JobState.FAILED.value,
            finished_at=func.now(),
            lease_owner=None,
            lease_expires_at=None,
            error_code=JobErrorCode.INTERNAL.value,
            error_message="worker stopped responding too many times",
        )
    ).rowcount
    conn.execute(
        update(JOBS)
        .where(JOBS.c.state == JobState.QUEUED.value, JOBS.c.cancel_requested.is_(True))
        .values(
            state=JobState.CANCELLED.value,
            finished_at=func.now(),
            error_code=JobErrorCode.CANCELLED.value,
            error_message="cancelled",
        )
    )
    return requeued, failed


def runnable_count(conn: Connection) -> int:
    return int(
        conn.execute(
            select(func.count())
            .select_from(JOBS)
            .where(
                JOBS.c.state == JobState.QUEUED.value,
                JOBS.c.run_after <= func.now(),
                JOBS.c.attempt < JOBS.c.max_attempts,
            )
        ).scalar_one()
    )
