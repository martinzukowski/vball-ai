"""VBall's cloud backend on Modal: the API plus workers that only run while there is work.

    modal deploy infra/modal/vball_app.py      (from the repo root, after `modal setup`)

* ``api``        FastAPI behind Modal's HTTPS URL; scales to zero between uses.
* ``dispatch``   runs every minute on a tiny CPU slice: if jobs are waiting (or a worker died
                 holding one), it starts the right worker. Nothing else runs while idle.
* ``cpu_worker`` ingest + highlight exports (ffmpeg), 4 cores.
* ``gpu_worker`` rally analysis (DINOv2 on a T4 GPU).

Configuration comes from the Modal secret ``vball`` (same VBALL_* variables as .env; see
docs/DEPLOY.md). The Starter plan's free monthly compute covers one user's matches.
"""

from __future__ import annotations

from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parents[2]
SRC = {
    "packages/vball_core/src": "/app/core",
    "services/api/src": "/app/api",
    "services/worker/src": "/app/worker",
    "ml/src": "/app/ml",
}
PYTHONPATH = ":".join(SRC.values())

app = modal.App("vball")
secret = modal.Secret.from_name("vball")
cache = modal.Volume.from_name("vball-cache", create_if_missing=True)  # DINOv2 weights


def with_source(image: modal.Image) -> modal.Image:
    image = image.env({"PYTHONPATH": PYTHONPATH, "VBALL_ENV": "production"})
    for local, remote in SRC.items():
        image = image.add_local_dir(ROOT / local, remote, ignore=["**/__pycache__"])
    return image


api_image = with_source(
    modal.Image.debian_slim(python_version="3.12").pip_install_from_requirements(
        str(ROOT / "infra/modal/requirements-api.txt")
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


@app.function(image=api_image, secrets=[secret], scaledown_window=300, timeout=120)
@modal.concurrent(max_inputs=50)
@modal.asgi_app()
def api():  # type: ignore[no-untyped-def]
    from vball_api.main import create_app

    return create_app()


def _run_worker(kinds: list[str], worker_id: str) -> int:
    from vball_core.db.enums import JobKind
    from vball_core.logging import configure_logging
    from vball_core.settings import get_settings
    from vball_worker.runner import Worker

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    done = Worker(settings, kinds=[JobKind(k) for k in kinds], worker_id=worker_id).run_until_idle()
    cache.commit()  # keep downloaded model weights for the next run
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
    return _run_worker(["ingest", "highlight"], "modal-cpu")


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


@app.function(image=api_image, secrets=[secret], schedule=modal.Period(minutes=1), timeout=60)
def dispatch() -> dict[str, int]:
    """Start a worker for each kind of waiting job. A job whose worker died (lease expired)
    counts as waiting: the worker's housekeeping puts it back in the queue."""
    from sqlalchemy import create_engine, func, or_, select

    from vball_core.db.enums import JobState
    from vball_core.db.models import ProcessingJob as J
    from vball_core.settings import get_settings

    engine = create_engine(get_settings().database_dsn(), pool_size=1)
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
        busy = conn.execute(  # a live worker drains the queue itself before it exits
            select(J.kind)
            .where(J.state == JobState.RUNNING, J.lease_expires_at >= func.now())
            .distinct()
        ).all()
    engine.dispose()
    name = lambda k: str(getattr(k, "value", k))  # noqa: E731
    waiting = {name(k): int(n) for k, n in rows}
    running = {name(k) for (k,) in busy}
    cpu_kinds = {"ingest", "highlight"}
    if any(waiting.get(k) for k in cpu_kinds) and not running & cpu_kinds:
        cpu_worker.spawn()
    if waiting.get("analyze") and "analyze" not in running:
        gpu_worker.spawn()
    return waiting
