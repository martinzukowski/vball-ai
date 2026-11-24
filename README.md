# VBall

Cloud volleyball video analysis: upload a match from your phone, get rallies (and later
players, ball tracking, actions and statistics) computed on remote workers.

**Status: milestones M0–M3 (foundation, database, sign-in, uploads).** No analysis yet.
Progress log and decisions: [docs/PROGRESS.md](docs/PROGRESS.md). Sign-in needs a Supabase project: see [docs/setup-supabase.md](docs/setup-supabase.md).
See [ARCHITECTURE.md](ARCHITECTURE.md) for the design and
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the milestone plan.

## Layout

| Path | What |
|---|---|
| `apps/web` | Next.js + TypeScript frontend (Vercel) |
| `services/api` | FastAPI backend |
| `packages/vball_core` | Shared Python: settings, logging, database models |
| `migrations` | Alembic database migrations |
| `infra/docker` | Local dev stack (Postgres, SeaweedFS S3, API) and Dockerfiles |

## Local development

Requirements: Docker, [uv](https://docs.astral.sh/uv/), Node 22+ and pnpm.

```sh
cp .env.example .env
uv sync                       # Python deps (Python 3.12 is fetched automatically)
pnpm install                  # web deps
docker compose -f infra/docker/compose.yaml up --build   # Postgres, S3 stand-in, migrations, API
pnpm web:dev                  # http://localhost:3000
```

API: http://localhost:8000/healthz (liveness), `/readyz` (database reachable), `/docs`.

### Tests and checks

```sh
uv run ruff check . && uv run ruff format --check .
uv run python -m mypy
VBALL_TEST_DATABASE_URL=postgresql+psycopg://vball:vball-local-only@localhost:5432/postgres \
  uv run python -m pytest
pnpm web:lint && pnpm web:typecheck && pnpm web:build
```

Database tests create and drop a throwaway database per run. They are skipped when
`VBALL_TEST_DATABASE_URL` is unset, except in CI, where that is an error.

### Database migrations

```sh
uv run python -m alembic upgrade head
uv run python -m alembic revision --autogenerate -m "describe change"   # then review by hand
uv run python -m alembic check                                          # models match migrations
```

### API client

The web app's API types are generated from the FastAPI OpenAPI schema; CI fails if they drift:

```sh
uv run vball-api-openapi apps/web/openapi.json && pnpm api:types
```

## Principles

No placeholder models, simulated predictions or fake progress in user-facing paths; heuristic
results are labelled as heuristics; no accuracy claim without a benchmark report.
