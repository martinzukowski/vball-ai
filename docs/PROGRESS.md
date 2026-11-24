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
| M4 Job system + ingest worker | In progress | Queue done + tested; worker not started |
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

### M4 — Job system + ingest worker (in progress, 2026-11-24)
Done:
- `packages/vball_core/src/vball_core/jobs.py`: Postgres queue. Idempotent `enqueue`,
  `claim` (SKIP LOCKED, increments attempt, 5-min lease), `heartbeat` (extends lease, raises
  `LeaseLostError` if lost/cancelled; progress capped at 0.99), `succeed`/`fail` (only by the
  lease holder; retryable failures back off 30s·2^n up to 10 min), `sweep` (requeue expired
  leases or fail after max attempts; finalise cancelled queued jobs), `runnable_count`.
- 12 tests in `packages/vball_core/tests/test_jobs.py` incl. 8 threads racing for 30 jobs.
- Migration `0003`: `videos.proxy_size_bytes`. Settings: `proxy_height` (720),
  `keep_originals` (false), `max_video_duration_s` (4 h), `worker_poll_interval_s`,
  `worker_scratch_dir`.

**Resume here — remaining M4 steps:**
1. `services/worker` package (`vball_worker`): add to the uv workspace (`pyproject.toml`
   members + sources) and mypy `files`.
   - `media.py`: ffprobe JSON → duration, fps, displayed width/height, rotation (display
     matrix), codecs, HDR transfer (`arib-std-b67`/`smpte2084`). Transcode proxy:
     `scale=-2:'min(720,trunc(ih/2)*2)'`, libx264 veryfast CRF 26, yuv420p, `-fpsmax 60`,
     keyframe every 2 s (`-force_key_frames expr:gte(t,n_forced*2)`), AAC 96k stereo,
     `+faststart`, `-progress pipe:1` for progress. iPhone HDR → tone-map with zscale/tonemap
     (check the Debian ffmpeg has libzimg). Poster JPEG from the proxy at ~10% of duration.
   - `ingest.py`: download original (boto3 `download_file`) → sha256 → probe (non-video,
     zero duration, > max duration → `INPUT_INVALID`, not retried) → proxy + poster → upload to
     `proxies/{owner}/{video}.mp4` / `posters/...jpg` → set metadata, `proxy_key`,
     `proxy_size_bytes`, status `ready` → delete original unless `keep_originals`
     (`original_deleted_at`). Video status: `ingesting` on claim, `failed` + friendly
     `error_message` when the job fails for good.
   - `runner.py`: loop = sweep → reconcile videos stuck in `ingesting` whose ingest job failed
     → claim → run handler with a heartbeat thread (20 s) → succeed/fail; classify errors
     (input invalid / transient boto+network / internal); write `job_stage_metrics` per stage
     (wall, cpu via `os.times()` incl. children). `__main__.py`: polling loop.
   - Storage additions needed: `download(key, path)`, `upload(path, key, content_type)`.
2. API: in `complete_upload`, enqueue the ingest job in the same transaction
   (`await db.run_sync(lambda s: jobs.enqueue(s.connection(), ...))`, pipeline `ingest-1`).
   Add latest-job info (state, stage, progress, error) to `VideoOut`; `stored_bytes` must count
   `proxy_size_bytes` and only originals not yet deleted.
3. `infra/docker/worker.Dockerfile` (python:3.12-slim + apt ffmpeg) and a `worker` service in
   compose (same S3 env as the api service).
4. Tests: ingest on a generated clip (`ffmpeg -f lavfi -i testsrc2=size=1920x1080:rate=30
   -f lavfi -i sine -t 5 ...`) → proxy ≤720p, metadata, original deleted, status ready;
   garbage bytes → failed `input_invalid`. CI: `sudo apt-get install -y ffmpeg` in the Python job.
5. Web: show "Preparing… NN%" from the job and poll every 5 s while any video is
   uploaded/ingesting; play the proxy.
6. Measure wall/CPU time per minute of video on a real match and record it here (replaces the
   cost estimates in ARCHITECTURE.md §12).

## Decisions to remember

- **Storage budget.** Originals from phones are large (≈4–8 GB per hour at 1080p). Plan for M4:
  after a successful ingest, keep a ~720p H.264 proxy (≈1 GB/hour) and delete the original, so
  several matches fit in the free tier. A 4K hour-long original (>9 GB) can't be uploaded at all
  under the cap; record 1080p.
- **Local S3** is SeaweedFS; it allows any origin by default, R2 does not (hence the script).
- **Tests never read `.env`** (`conftest.py` disables it) so local config can't change results.

## Local environment notes

- `.env` (repo root) and `apps/web/.env.local` hold local config; both git-ignored, so they
  must be recreated on a new machine:
  - `.env`: copy `.env.example`, set `VBALL_SUPABASE_URL=https://hkfzpbzivnnwspckzjbi.supabase.co`
    and `VBALL_CORS_ORIGINS=http://localhost:3000,http://localhost:3100`.
  - `apps/web/.env.local`: `NEXT_PUBLIC_API_URL=http://localhost:8000`,
    `NEXT_PUBLIC_SUPABASE_URL=https://hkfzpbzivnnwspckzjbi.supabase.co`,
    `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=` (Supabase → Project Settings → API Keys →
    publishable key), `NEXT_PUBLIC_AUTH_GOOGLE_ENABLED=false`.
- New machine setup: install Docker, uv, Node 22+, pnpm, ffmpeg; `uv sync`; `pnpm install`;
  `docker compose -f infra/docker/compose.yaml --env-file .env up -d --build`;
  `pnpm --filter @vball/web dev -p 3100`. If the new machine's web app runs on another
  port, add `http://localhost:<port>/auth/callback` to Supabase → Authentication → URL
  Configuration.
- Pending from the owner: Cloudflare R2 account + `vball` bucket + API token (see M3 notes);
  or say to switch to Backblaze B2 (no card needed).
- Port 3000 is taken on the dev PC; the web app runs on 3100 (`pnpm --filter @vball/web dev -p 3100`).
- Test env vars for S3 upload tests are in `.env.example` (`VBALL_TEST_S3_*`).
- Windows blocks running `.venv\Scripts\*.exe`; run tools as `uv run python -m pytest`,
  `python -m mypy`, `python -m alembic`.
