"""Per-job context: progress reporting, cancellation, and per-stage resource metrics."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.postgresql import insert

from vball_core import jobs
from vball_core.db.models import JobStageMetric
from vball_core.logging import get_logger

log = get_logger("vball_worker")


def _cpu_seconds() -> float:
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


def _peak_child_mem_mb() -> float | None:
    if sys.platform == "win32":
        return None
    import resource

    kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss  # KiB on Linux
    return kb / 1024.0


@dataclass
class JobResult:
    """What a handler hands back: DB writes to commit atomically with the job's success,
    and best-effort cleanup to run after that commit."""

    finalize: Callable[[Connection], None]
    after_commit: Callable[[], None] | None = None


@dataclass
class JobContext:
    job: jobs.ClaimedJob
    worker_id: str
    engine: Engine
    worker_kind: str = "cpu"
    _progress: float = 0.0
    _stage: str | None = None
    _lost: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def progress(self, value: float) -> None:
        """Record progress (0..1) and abort if the job was cancelled or the lease lost."""
        with self._lock:
            self._progress = value
        self.check()

    def check(self) -> None:
        if self._lost:
            raise jobs.LeaseLostError(self._lost)

    @contextmanager
    def stage(self, name: str, frames: int | None = None) -> Iterator[None]:
        self.check()
        with self._lock:
            self._stage = name
        started = datetime.now(UTC)
        t0, cpu0 = time.monotonic(), _cpu_seconds()
        log.info("stage_started", job_id=str(self.job.id), stage=name)
        yield
        wall, cpu = time.monotonic() - t0, _cpu_seconds() - cpu0
        log.info("stage_finished", job_id=str(self.job.id), stage=name, wall_s=round(wall, 2))
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    insert(JobStageMetric)
                    .values(
                        job_id=self.job.id,
                        attempt=self.job.attempt,
                        stage=name,
                        started_at=started,
                        finished_at=datetime.now(UTC),
                        wall_s=wall,
                        cpu_s=cpu,
                        peak_mem_mb=_peak_child_mem_mb(),
                        frames_processed=frames,
                        worker_kind=self.worker_kind,
                    )
                    .on_conflict_do_nothing()
                )
        except Exception as exc:  # metrics must never fail a job
            log.warning("stage_metric_failed", error=type(exc).__name__)

    # --- heartbeat ----------------------------------------------------------------------
    def heartbeat_once(self) -> None:
        with self._lock:
            progress, stage = self._progress, self._stage
        try:
            with self.engine.begin() as conn:
                jobs.heartbeat(conn, self.job.id, self.worker_id, progress=progress, stage=stage)
        except jobs.LeaseLostError as exc:
            self._lost = str(exc)
            log.warning("lease_lost", job_id=str(self.job.id), reason=str(exc))
        except Exception as exc:  # DB blip: try again next beat; the lease has slack
            log.warning("heartbeat_failed", job_id=str(self.job.id), error=type(exc).__name__)

    @contextmanager
    def heartbeating(self, interval_s: float) -> Iterator[None]:
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(interval_s):
                self.heartbeat_once()

        thread = threading.Thread(target=loop, name=f"heartbeat-{self.job.id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=10)
