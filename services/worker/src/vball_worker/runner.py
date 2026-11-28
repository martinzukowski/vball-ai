"""Worker loop: recover stuck jobs, claim the next one, run its handler, record the outcome.

The same code runs as a long-lived poller (local Docker, any VM) or as "process until the queue
is empty" (serverless invocations, tests).
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import Engine, and_, create_engine, exists, update

from vball_core import jobs
from vball_core.db.enums import ExportStatus, JobErrorCode, JobKind, JobState, VideoStatus
from vball_core.db.models import HighlightExport, ProcessingJob, Video
from vball_core.logging import configure_logging, get_logger
from vball_core.settings import Settings
from vball_core.storage import ObjectStorage
from vball_worker.analyze import run_analyze
from vball_worker.context import JobContext, JobResult
from vball_worker.highlight import run_highlight
from vball_worker.ingest import run_ingest
from vball_worker.media import InvalidMediaError, MediaToolError

log = get_logger("vball_worker")

Handler = Callable[[JobContext, Settings, ObjectStorage], JobResult]
HANDLERS: dict[JobKind, Handler] = {
    JobKind.INGEST: run_ingest,
    JobKind.ANALYZE: run_analyze,
    JobKind.HIGHLIGHT: run_highlight,
}
HEARTBEAT_INTERVAL_S = 20.0


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class Worker:
    def __init__(
        self,
        settings: Settings,
        engine: Engine | None = None,
        storage: ObjectStorage | None = None,
        worker_id: str | None = None,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        kinds: list[JobKind] | None = None,
    ) -> None:
        self.settings = settings
        # Job kinds this worker takes (e.g. a GPU worker only runs analysis). Default: all.
        self.kinds = kinds or list(HANDLERS)
        self.engine = engine or create_engine(settings.database_dsn(), pool_pre_ping=True)
        self.storage = storage or ObjectStorage(settings)
        self.worker_id = worker_id or default_worker_id()
        self.heartbeat_interval_s = heartbeat_interval_s

    # --- housekeeping -------------------------------------------------------------------
    def housekeeping(self) -> None:
        with self.engine.begin() as conn:
            requeued, failed = jobs.sweep(conn)
            if requeued or failed:
                log.warning("swept_jobs", requeued=requeued, failed=failed)
            # A video whose ingest gave up (e.g. worker crashed on every attempt) must not stay
            # "preparing" forever.
            job = ProcessingJob
            active = exists().where(
                and_(
                    job.video_id == Video.id,
                    job.kind == JobKind.INGEST,
                    job.state.in_([JobState.QUEUED, JobState.RUNNING]),
                )
            )
            failed_ingest = exists().where(
                and_(
                    job.video_id == Video.id,
                    job.kind == JobKind.INGEST,
                    job.state == JobState.FAILED,
                )
            )
            conn.execute(
                update(Video)
                .where(Video.status == VideoStatus.INGESTING, ~active, failed_ingest)
                .values(
                    status=VideoStatus.FAILED,
                    error_message="Processing failed repeatedly. Try uploading again.",
                )
            )

    # --- one job --------------------------------------------------------------------------
    def run_once(self) -> bool:
        """Claim and process one job. Returns False if there was nothing to do."""
        with self.engine.begin() as conn:
            job = jobs.claim(conn, self.worker_id, kinds=self.kinds)
            if job is not None and job.kind is JobKind.INGEST:
                conn.execute(
                    update(Video)
                    .where(Video.id == job.video_id, Video.status != VideoStatus.DELETED)
                    .values(status=VideoStatus.INGESTING, error_message=None)
                )
        if job is None:
            return False

        ctx = JobContext(job=job, worker_id=self.worker_id, engine=self.engine)
        log.info("job_started", job_id=str(job.id), kind=job.kind.value, attempt=job.attempt)
        started = time.monotonic()
        try:
            with ctx.heartbeating(self.heartbeat_interval_s):
                result = HANDLERS[job.kind](ctx, self.settings, self.storage)
                ctx.check()
            with self.engine.begin() as conn:
                result.finalize(conn)
                jobs.succeed(conn, job.id, self.worker_id)
        except jobs.LeaseLostError as exc:
            log.warning("job_abandoned", job_id=str(job.id), reason=str(exc))
            return True
        except Exception as exc:
            self._record_failure(job, exc)
            return True
        log.info("job_succeeded", job_id=str(job.id), wall_s=round(time.monotonic() - started, 1))
        if result.after_commit:
            result.after_commit()
        return True

    def _record_failure(self, job: jobs.ClaimedJob, exc: Exception) -> None:
        if storage_refused(exc):
            # Usually the storage provider's free daily download cap (Backblaze B2 resets it at
            # 00:00 GMT): wait for the reset instead of burning the job's attempts.
            until = next_storage_reset()
            log.warning("storage_refused_deferring", job_id=str(job.id), until=until.isoformat())
            try:
                with self.engine.begin() as conn:
                    jobs.defer(
                        conn,
                        job.id,
                        self.worker_id,
                        until,
                        JobErrorCode.TRANSIENT,
                        "Storage's free daily download limit is used up; processing continues "
                        "automatically after it resets (midnight GMT).",
                    )
            except jobs.LeaseLostError:
                log.warning("failure_not_recorded_lease_lost", job_id=str(job.id))
            return
        code, retryable, message = classify(exc)
        log.error(
            "job_failed",
            job_id=str(job.id),
            code=code.value,
            retryable=retryable,
            error=type(exc).__name__,
            detail=str(exc)[:500],
        )
        try:
            with self.engine.begin() as conn:
                state = jobs.fail(conn, job.id, self.worker_id, code, message, retryable=retryable)
                if state is JobState.FAILED and job.kind is JobKind.HIGHLIGHT:
                    conn.execute(
                        update(HighlightExport)
                        .where(HighlightExport.job_id == job.id)
                        .values(status=ExportStatus.FAILED)
                    )
                if state is JobState.FAILED and job.kind is JobKind.INGEST:
                    conn.execute(
                        update(Video)
                        .where(Video.id == job.video_id, Video.status != VideoStatus.DELETED)
                        .values(status=VideoStatus.FAILED, error_message=message[:500])
                    )
        except jobs.LeaseLostError:
            log.warning("failure_not_recorded_lease_lost", job_id=str(job.id))

    # --- loops ----------------------------------------------------------------------------
    def run_until_idle(self, max_jobs: int | None = None) -> int:
        """Process jobs until none are runnable. Returns how many were processed."""
        self.housekeeping()
        done = 0
        while (max_jobs is None or done < max_jobs) and self.run_once():
            done += 1
        return done

    def run_forever(self) -> None:
        log.info("worker_started", worker_id=self.worker_id)
        last_housekeeping = 0.0
        while True:
            try:
                if time.monotonic() - last_housekeeping > 30:
                    self.housekeeping()
                    last_housekeeping = time.monotonic()
                if not self.run_once():
                    time.sleep(self.settings.worker_poll_interval_s)
            except Exception as exc:  # DB down etc.: back off, keep the process alive
                log.error("worker_loop_error", error=type(exc).__name__, detail=str(exc)[:300])
                time.sleep(10)


def storage_refused(exc: Exception) -> bool:
    """Storage answered 403 / AccessDenied (B2: download or transaction cap reached)."""
    if not isinstance(exc, ClientError):
        return False
    err = exc.response.get("Error", {})
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status == 403 or err.get("Code") in ("403", "AccessDenied")


def next_storage_reset(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)


def classify(exc: Exception) -> tuple[JobErrorCode, bool, str]:
    """(error code, retryable, message shown to the user)."""
    if isinstance(exc, InvalidMediaError):
        return JobErrorCode.INPUT_INVALID, False, str(exc)
    if isinstance(exc, (BotoCoreError, ClientError, ConnectionError, TimeoutError)):
        return JobErrorCode.TRANSIENT, True, "A storage or network error interrupted processing."
    if isinstance(exc, subprocess.TimeoutExpired):
        return JobErrorCode.TRANSIENT, True, "Processing timed out."
    if isinstance(exc, MediaToolError):
        return JobErrorCode.INTERNAL, True, "The video couldn't be converted."
    return JobErrorCode.INTERNAL, True, "Something went wrong while processing the video."


def main_loop(settings: Settings) -> None:
    configure_logging(settings.log_level, settings.log_json)
    Worker(settings).run_forever()


__all__ = ["HANDLERS", "Worker", "classify", "main_loop"]
