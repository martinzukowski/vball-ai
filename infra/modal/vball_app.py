"""VBall's video processing on Modal: workers that only run while there is work.

    uvx modal deploy infra/modal/vball_app.py      (from the repo root, after `modal setup`)

* ``wake``       HTTPS endpoint the API calls right after queuing a job (bearer token
                 VBALL_WORKER_WAKE_TOKEN). Starts the right worker(s) and returns at once.
* ``sweep``      every 30 min: the same check, for anything missed (e.g. a deferred job whose
                 storage cap has reset, or a worker that died holding a job).
* ``cpu_worker`` ingest (playback copy) + highlight exports, 4 cores.
* ``gpu_worker`` rally analysis (DINOv2 on a T4 GPU).

The website and API stay on Vercel. Configuration comes from the Modal secret ``vball`` (the
VBALL_* lines of infra/production.env plus VBALL_WORKER_WAKE_TOKEN; see docs/DEPLOY.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import modal

try:
    from fastapi import Request
except ImportError:  # deploying from an environment without FastAPI; the container has it
    Request = Any  # type: ignore[misc,assignment]

ROOT = Path(__file__).resolve().parents[2]
SRC = {
    "packages/vball_core/src": "/app/core",
    "services/worker/src": "/app/worker",
    "ml/src": "/app/ml",
}
CPU_KINDS = {"ingest", "highlight"}

app = modal.App("vball")
secret = modal.Secret.from_name("vball")
cache = modal.Volume.from_name("vball-cache", create_if_missing=True)  # DINOv2 weights


def with_source(image: modal.Image) -> modal.Image:
    image = image.env({"PYTHONPATH": ":".join(SRC.values()), "VBALL_ENV": "production"})
    for local, remote in SRC.items():
        image = image.add_local_dir(ROOT / local, remote, ignore=["**/__pycache__"])
    return image


control_image = with_source(
    modal.Image.debian_slim(python_version="3.12").pip_install_from_requirements(
        str(ROOT / "deploy/api/requirements.txt")  # FastAPI + SQLAlchemy + psycopg, no ML
    )
)
worker_image = with_source(
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install_from_requirements(str(ROOT / "infra/modal/requirements-worker.txt"))
    # PyPI's Linux torch wheel is the CUDA build (the repo pins the CPU build for laptops).
    .pip_install("torch==2.15.0")
    .env({"HF_HOME": "/cache/hf", "VBALL_WORKER_SCRATCH_DIR": "/scratch"})
    .run_commands("mkdir -p /scratch")
)


def waiting_work() -> tuple[dict[str, int], set[str]]:
    """Runnable jobs per kind (incl. ones whose worker died) and kinds a live worker holds."""
    from sqlalchemy import create_engine, func, or_, select

    from vball_core.db.enums import JobState
    from vball_core.db.models import ProcessingJob as J
    from vball_core.settings import get_settings

    engine = create_engine(get_settings().database_dsn(), pool_size=1)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                select(J.kind, func.count())
                .where(
                    or_(
                        (J.state == JobState.QUEUED)
                        & (J.run_after <= func.now())
                        & (J.attempt < J.max_attempts)
                        & J.cancel_requested.is_(False),
                        (J.state == JobState.RUNNING) & (J.lease_expires_at < func.now()),
                    )
                )
                .group_by(J.kind)
            ).all()
            busy = conn.execute(
                select(J.kind)
                .where(J.state == JobState.RUNNING, J.lease_expires_at >= func.now())
                .distinct()
            ).all()
    finally:
        engine.dispose()

    def name(k: Any) -> str:
        return str(getattr(k, "value", k))

    return {name(k): int(n) for k, n in rows}, {name(k) for (k,) in busy}


def start_workers() -> dict[str, Any]:
    """Spawn a worker for each kind of waiting job unless one is already busy with it (a live
    worker drains the queue before it exits)."""
    waiting, running = waiting_work()
    started = []
    if any(waiting.get(k) for k in CPU_KINDS) and not running & CPU_KINDS:
        cpu_worker.spawn()
        started.append("cpu")
    if waiting.get("analyze") and "analyze" not in running:
        gpu_worker.spawn()
        started.append("gpu")
    return {"waiting": waiting, "started": started}


def _run_worker(kinds: list[str], worker_id: str) -> int:
    from vball_core.db.enums import JobKind
    from vball_core.logging import configure_logging
    from vball_core.settings import get_settings
    from vball_worker.runner import Worker

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    done = Worker(settings, kinds=[JobKind(k) for k in kinds], worker_id=worker_id).run_until_idle()
    cache.commit()  # keep downloaded model weights for the next run
    start_workers()  # e.g. ingest just queued an analysis for the GPU worker
    return done


@app.function(
    image=worker_image,
    secrets=[secret],
    volumes={"/cache": cache},
    cpu=4.0,
    memory=4096,
    timeout=3 * 3600,
    max_containers=1,
)
def cpu_worker() -> int:
    return _run_worker(sorted(CPU_KINDS), "modal-cpu")


@app.function(
    image=worker_image,
    secrets=[secret],
    volumes={"/cache": cache},
    gpu="T4",
    cpu=2.0,
    memory=8192,
    timeout=3600,
    max_containers=1,
)
def gpu_worker() -> int:
    return _run_worker(["analyze"], "modal-gpu")


@app.function(image=control_image, secrets=[secret], timeout=60)
@modal.fastapi_endpoint(method="POST")
def wake(request: Request) -> dict[str, Any]:
    import hmac
    import os

    from fastapi import HTTPException

    expected = os.environ.get("VBALL_WORKER_WAKE_TOKEN", "")
    given = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not expected or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
    return start_workers()


@app.function(image=control_image, secrets=[secret], schedule=modal.Period(minutes=30), timeout=60)
def sweep() -> dict[str, Any]:
    return start_workers()
