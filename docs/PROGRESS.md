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
| M4 Job system + ingest worker | Done locally | Web progress UI not yet seen while signed in |
| M5 Heuristic rally detection in worker | In progress | Detector + features done; job/API/UI next |
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

### M4 — Job system + ingest worker (2026-11-24 → 25)
- Queue: `packages/vball_core/src/vball_core/jobs.py` (Postgres, SKIP LOCKED claims, 5-min
  leases + heartbeats, retries with backoff, sweep for crashed workers, cancel). 12 tests incl.
  8 threads racing for 30 jobs.
- Worker: `services/worker/src/vball_worker/` — `media.py` (ffprobe validation, proxy
  transcode, poster), `ingest.py`, `context.py` (progress, cancellation, per-stage metrics in
  `job_stage_metrics`), `runner.py` (loop, error classification, video status reconcile).
  Runs as `python -m vball_worker` (Docker `worker` service) or `Worker.run_until_idle()`.
- Ingest: download original → sha256 → ffprobe (non-video / no duration / > 4 h → failed,
  not retried, friendly message) → proxy: short side ≤720 px (portrait stays portrait),
  H.264 CRF 26 veryfast, ≤60 fps, keyframe every 2 s, AAC 96k, faststart, metadata stripped
  (GPS), iPhone HDR (HLG/PQ) tone-mapped to SDR → poster JPEG → upload → mark ready
  atomically with job success → delete original (`VBALL_KEEP_ORIGINALS=true` to keep).
- API: upload completion enqueues ingest in the same transaction; videos carry
  `processing` (latest job: state, stage, progress, error), `poster_url`, `original_available`;
  `POST /videos/{id}/retry` for failed videos; delete cancels running jobs; storage usage counts
  proxies. Pipeline versions in `vball_core/pipelines.py`.
- Web: library polls every 4 s while anything is processing and shows stage + %; poster
  thumbnails; Retry button; `/videos/[id]` plays the proxy.
- Verified: 137 Python tests (21 worker tests with real ffmpeg + S3 + Postgres: rotated and HDR
  clips, junk file, missing original → retries then fails, cancel mid-run), CI installs ffmpeg
  and checks the worker image has zscale/tonemap. Docker worker processed a 20 s 1080p clip
  queued for the owner: ready in 2.3 s, proxy 2.5 MB, original deleted.
- Not yet measured: a real match. Synthetic test patterns compress unrealistically well, so
  don't use the 2.5 MB/20 s figure for storage planning.

### M5 — Rally detection (in progress, 2026-11-25)
Done:
- `services/worker/src/vball_worker/rally/heuristic.py`: Python port of the original
  `detect.js` ("heuristic-v1", hand-written rules, not ML). Parity test runs the original JS
  (`reference/rally-cutter-js/`) under Node on 60 synthetic matches x 3 sensitivities and requires
  identical output; 4 of 6 deliberate mutations are caught (the 2 survivors are `>=` vs `>`
  boundary tweaks that float timestamps never hit exactly).
- `rally/features.py`: deterministic motion (play-area polygon mask, 256x144 @ 5 fps), whistle
  band audio, camera-move detection.
- Sample match (owner's 21-min 1080p60 set, kept outside the repo): proxy 6.3 min on the dev
  laptop, 178 MB (~0.5 GB/h); features 60 s; 55 whistles, 57 rallies, 9.8 of 21.1 min kept;
  3 camera moves detected. 12-frame spot check looked right; accuracy NOT measured (no labels).

**Resume here:** migration `videos.play_area` (normalised polygon) → analyze job (auto after
ingest; download proxy, features artifact `artifacts/.../features.json.gz`, detect, write
analysis_run + rallies + camera_segments) → API `PUT /videos/{id}/play-area`,
`GET /videos/{id}/rallies` → review screen in the requested layout: video left, right panel with
tabs (Rallies / Longest / Starred), rows "6:11 - 6:18 | Rally #1" with edit + star. Actions,
scoreboard and stats tabs only once real models exist.

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
