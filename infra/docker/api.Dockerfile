# API image. Build from the repo root:
#   docker build -f infra/docker/api.Dockerfile .
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/vball_core/pyproject.toml packages/vball_core/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/worker/pyproject.toml services/worker/pyproject.toml
COPY ml/pyproject.toml ml/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-workspace --package vball-api

COPY packages/vball_core packages/vball_core
COPY services/api services/api
COPY alembic.ini ./
COPY migrations migrations
RUN uv sync --frozen --no-dev --package vball-api

RUN useradd --create-home --uid 10001 app
USER app
ENV PATH="/opt/venv/bin:$PATH"

EXPOSE 8000
CMD ["uvicorn", "vball_api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
