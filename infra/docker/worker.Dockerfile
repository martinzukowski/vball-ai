# Worker image (ffmpeg + Python). Build from the repo root:
#   docker build -f infra/docker/worker.Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY packages/vball_core/pyproject.toml packages/vball_core/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/worker/pyproject.toml services/worker/pyproject.toml
COPY ml/pyproject.toml ml/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-workspace --package vball-worker

COPY packages/vball_core packages/vball_core
COPY services/worker services/worker
COPY ml ml
RUN uv sync --frozen --no-dev --package vball-worker

RUN useradd --create-home --uid 10001 app && mkdir -p /scratch && chown app /scratch
USER app
ENV PATH="/opt/venv/bin:$PATH" VBALL_WORKER_SCRATCH_DIR=/scratch HF_HOME=/scratch/hf

CMD ["python", "-m", "vball_worker"]
