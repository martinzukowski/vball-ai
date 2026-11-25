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
| M5 Heuristic rally detection in worker | Done locally | Play-area editor UI comes in M6 |
| M6 Review/correction web UI | In progress | Edits + review API done; editing UI next |
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

Also done (part 2):
- Migration `0004`: `videos.play_area` (normalised polygon, 3-16 points).
- Worker `analyze.py` (job kind `analyze`, pipeline `rally-heuristic-1`), enqueued automatically
  when ingest succeeds. Writes features artifact, analysis_run (made active atomically),
  camera_segments, rallies (`confidence` NULL: heuristic scores aren't probabilities).
- API: `GET /videos/{id}/rallies`, `PUT /videos/{id}/play-area` (re-runs detection),
  `POST /videos/{id}/analyze {sensitivity -5..5}`.
- Review screen `/videos/[id]` in the owner's requested layout (video left, rally panel right
  with Rallies / Longest / Starred tabs, "m:ss – m:ss | Rally #n" rows, current rally
  highlighted, star), "Skip dead time" playback, Fewer/More sensitivity. Stars are per-browser
  (localStorage) until M6. Actions/scoreboard/stats intentionally absent (no model yet).
- Bug caught by tests: JSONB `None` was stored as JSON `null`, violating a CHECK; JSONB columns
  that may be NULL now use `none_as_null=True`.

### M6 — Corrections → training labels (in progress, 2026-11-25)
- Owner direction (2026-11-25): no hand-tuning; learned ML/CV that improves itself through
  gated retraining (promote only if better on unseen matches). See ARCHITECTURE §8.
- Heuristic on the sample match is poor on short rallies: late ends (motion never settles
  after points), missed serves, false rallies. Owner's Balltime rally list for the same video turned
  out to use a condensed timeline (2-3 s between rallies, ends at 10:55 of a 21:03 video), so it
  can't be compared position-by-position (an earlier "3 missed" comparison was wrong). Totals:
  Balltime 47 rallies / 9.0 min / median 10 s; ours 48 / 8.7 min / median 10 s. Owner-observed
  boundary errors still stand. Balltime output is a reference only, never training data.
- `vball_core/rallies.py`: effective rallies = predictions + append-only edits (add / modify /
  delete); user-added rallies survive re-analysis. API: `POST /videos/{id}/rallies/edits`
  (no overlaps, 404 for others' videos), `POST /videos/{id}/review` freezes the corrected list
  into `rally_label_sets` (migration 0005) with the model's original bounds kept.
- Next: editing UI (drag edges, set start/end to playhead, add, delete, mark reviewed,
  play-area editor) → owner labels this match → dataset builder + eval harness → learned
  rally model (frozen video embeddings + temporal model) → gated auto-retraining.

### Reference labels from Balltime (2026-11-25)
- Owner's choice (personal project): use Balltime's rally cutting for the sample match as the
  reference. Balltime only exports its condensed rally video (10:58); `ml/eval/align_reference_cut.py`
  aligns that cut to the original by frame fingerprints: all 47 rallies matched in order
  (similarity 0.98-1.00), about +/-1 s (list times are whole seconds).
- Stored as a `rally_label_sets` row (source `reference:balltime`) and shown as the match's
  active rally list (pipeline `reference-import-1`). Not hand-verified by the owner.
- **Baseline, heuristic-v1 vs this reference:** F1 0.40 at tIoU>=0.5 (19/47), 0.21 at >=0.7;
  median start error 0.8 s, end error 4.1 s (ends late). This is the bar the learned model must beat.
- Next: learned rally model (frozen pretrained frame embeddings + audio -> temporal model),
  trained/evaluated with held-out time blocks of this match until more matches exist.

### Learned rally model, first result (2026-11-25)
- `ml/` package (CPU PyTorch, Transformers): `features.py` (frozen DINOv2-small CLS embedding of
  the play-area crop at 5 fps + 32 log audio bands), `rally_model.py` (dilated temporal CNN,
  ~6 s context each side, per-step P(rally); generic decoding: smooth, 0.5 threshold, join gaps
  <1 s, drop <2 s), `evaluate.py` (Hungarian tIoU F1, boundary errors), `crossval.py`.
- Sample match, 4-fold blocked CV with 10 s guard bands, 3 seeds, vs the aligned Balltime
  reference: **learned F1@0.5 0.99, F1@0.7 0.97, median start err 0.2 s, end err 0.4 s**;
  heuristic-v1 F1@0.5 0.40 / 0.21. Per-fold learned 0.97-1.00, heuristic 0.28-0.52.
- Caveats: one match, one gym, one camera; the reference is Balltime (~+/-1 s), not
  hand-verified. Within-match CV can't show generalisation to other venues.
- Cost: feature extraction 12 min CPU for 21 min of video on the dev laptop (GPU later).
- Next: train on all labelled data, register the model (model_versions + artifact), run it in
  the worker's analyze job with the heuristic as fallback; collect more varied matches.

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
