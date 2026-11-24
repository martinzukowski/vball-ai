# VBall progress log

The living record of what has been built, the decisions behind it, how it was verified, and
what comes next. Read this first when picking the project up. Design rationale lives in
[ARCHITECTURE.md](../ARCHITECTURE.md); the original milestone plan in
[IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md).

## Scope (set 2026-11-23)

- **One user** (the owner). No multi-tenant scaling, billing tiers or per-user rate limiting.
  Authorization still checks ownership on every resource, because it's cheap and prevents bugs.
- **Free tiers only**: Vercel Hobby, Supabase Free, Cloudflare R2 (10 GB free), Modal Starter
  ($30/month credit). Storage is capped below the R2 free tier in the app itself.
- **Priority**: quickly cutting dead time out of match videos (rally detection + highlights),
  polished on a phone. Stats and player/ball/action ML are later and optional.

## Status

| Milestone | State | Notes |
|---|---|---|
| M0 Repo, tooling, local stack, CI | Done | |
| M1 Database schema + migrations | Done | |
| M2 Sign-in (Supabase) | Done locally | Phone test waits for deployment (M8) |
| M3 Direct uploads to object storage | Done locally | Needs Cloudflare R2 account for production |
| M4 Job system + ingest worker | Next | Probe, 720p proxy, delete original |
| M5 Heuristic rally detection in worker | | |
| M6 Review/correction web UI | | |
| M7 Server-side highlights | | |
| M8 Deployment (Vercel, Modal, Supabase, R2) | | |

## Log

### M0 — Foundation (2026-11-23)
- uv workspace (`packages/vball_core`, `services/api`), pnpm workspace (`apps/web`, Next.js 16).
- Docker Compose dev stack: Postgres 17, SeaweedFS as local S3 (MinIO stopped publishing
  images), migrations, API. `docker compose -f infra/docker/compose.yaml --env-file .env up -d --build`.
- CI (GitHub Actions): ruff, mypy --strict, pytest against Postgres, web lint/typecheck/build,
  OpenAPI→TypeScript client drift check, API image build, gitleaks.
- Verified: CI green; `/healthz` and `/readyz` respond from the compose stack.

### M1 — Database schema (2026-11-23)
- Migration `0001`: profiles, videos, uploads, processing_jobs, job_stage_metrics,
  model_versions, analysis_runs, camera_segments, rallies, rally_edits, highlight_exports.
- Invariants enforced in Postgres (CHECK + EXCLUDE): job state/lease consistency, no
  overlapping rallies within a run, one active analysis run per video, one production model per
  task. Row-level security on, deny-by-default (browser never queries Postgres).
- Lesson: `CHECK (a > 0 AND b > 0)` passes when a column is NULL; use `num_nulls`/`coalesce`.
  Tests caught this.

### M2 — Sign-in (2026-11-23)
- Supabase project `hkfzpbzivnnwspckzjbi` (free, us-east-2). Email magic link; Google optional
  behind `NEXT_PUBLIC_AUTH_GOOGLE_ENABLED`. Redirect URLs: localhost:3000 and :3100 callbacks.
- API verifies access tokens locally against the project JWKS (ES256). HS256/`none` rejected,
  key rotation handled, JWKS outage → 503. Profile row created on first request.
- Web: `@supabase/ssr`, `src/proxy.ts` refreshes the session and guards `/account`.
  Next 16 notes: `middleware` is now `proxy`; with Cache Components, session reads must sit in
  `<Suspense>` and call `await connection()` first (Supabase checks token expiry with `Date.now()`).
- Verified: real sign-in by the owner; API returned the profile; forged token against the real
  project rejected.

### M3 — Direct uploads (2026-11-24)
- Browser uploads straight to object storage with presigned multipart URLs; the API never
  touches video bytes. 16 MiB parts (larger for huge files, max 10,000 parts), 3 in parallel,
  5 attempts per part with backoff, URLs re-signed after 45 min, concurrent workers share one
  signing request. Engine: `apps/web/src/lib/upload/engine.ts` (pure, unit-tested).
- Resume: an "unfinished upload" marker (file name+size+mtime → video id) in localStorage. After
  a reload the user re-picks the same file; the engine asks the API which parts storage already
  has and uploads only the rest. Screen wake lock held during uploads on phones.
- API (`services/api/src/vball_api/routers/videos.py`): `POST /videos/uploads`,
  `GET /videos/{id}/upload`, `POST /videos/{id}/upload/parts`, `POST /videos/{id}/upload/complete`
  (server lists parts itself and checks every part + total size before completing; idempotent),
  `GET /videos`, `GET /videos/{id}`, `GET /videos/{id}/playback`, `DELETE /videos/{id}`.
- Free-tier guard: total stored bytes capped at 9 GiB (`VBALL_STORAGE_CAP_BYTES`; R2 free is
  10 GB). Migration `0002` sets the default profile quota to 9 GiB. Only .mp4/.m4v/.mov accepted
  (real content check by ffprobe in M4). Max single file 20 GiB.
- Production bucket setup script: `infra/r2/configure_bucket.py ORIGIN...` (CORS for the app
  origins + abort unfinished multipart uploads after 7 days).
- Verified: 101 Python tests (upload flow against real SeaweedFS: resume, missing parts, size
  mismatch, validation, cross-user 404s, delete), 7 Vitest tests for the engine, and a real
  40 MB 3-part upload + delete in the browser while signed in.

## Decisions to remember

- **Storage budget.** Originals from phones are large (≈4–8 GB per hour at 1080p). Plan for M4:
  after a successful ingest, keep a ~720p H.264 proxy (≈1 GB/hour) and delete the original, so
  several matches fit in the free tier. A 4K hour-long original (>9 GB) can't be uploaded at all
  under the cap; record 1080p.
- **Local S3** is SeaweedFS; it allows any origin by default, R2 does not (hence the script).
- **Tests never read `.env`** (`conftest.py` disables it) so local config can't change results.

## Local environment notes

- `.env` (repo root) and `apps/web/.env.local` hold local config; both git-ignored. Supabase
  URL and publishable key are set; no secrets are committed.
- Port 3000 is taken on the dev PC; the web app runs on 3100 (`pnpm --filter @vball/web dev -p 3100`).
- Test env vars for S3 upload tests are in `.env.example` (`VBALL_TEST_S3_*`).
- Windows blocks running `.venv\Scripts\*.exe`; run tools as `uv run python -m pytest`,
  `python -m mypy`, `python -m alembic`.
