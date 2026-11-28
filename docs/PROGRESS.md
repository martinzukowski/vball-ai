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
| M6 Review/correction web UI | Done locally | Trim/add/delete, mark reviewed, play-area editor |
| M7 Server-side highlights | Done locally | Export all/starred rallies as one MP4 |
| M8 Deployment (Vercel, Supabase, B2, GitHub Actions) | Deployed 2026-11-28; phone upload test pending | web https://vball-ai-jtsd.vercel.app, API https://vball-ai.vercel.app; docs/DEPLOY.md |

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
- Wired in (2026-11-25): `python -m vball_ml.train_rally MANIFEST OUT.pt --version V --metrics M
  --register` trains an ensemble on all labelled matches, uploads it to
  `models/rally/<V>/model.pt` and makes it the production rally model (previous -> retired),
  recording held-out metrics. The worker's analyze job uses the production model
  (`rally-learned-1`, detector `learned:<V>`, confidence = mean model probability, uncalibrated),
  verifies the artifact's sha256, caches it in-process, and falls back to heuristic-v1 with a
  recorded `fallback_reason` if there's no model or it fails. `ml` no longer imports the worker
  (worker -> ml dependency). Worker image now includes CPU PyTorch (~4.3 GB image; slim later).
- Production model `2026-11-25.1`: 3-seed ensemble trained on the sample match (registered in
  the local stack only; the cloud registry starts empty at deployment).
- Promotion gate today is manual (beats heuristic in held-out CV). Automated retrain + gate
  (only promote if better on unseen matches) comes once there is more than one labelled match.

### M6 — Editing screen (2026-11-25)
- `apps/web/src/app/videos/[id]/edit-tools.tsx`: for the selected rally, Start/End <- current
  time, +/-0.5 s nudges, delete (confirm); add a rally around the current time; Mark reviewed
  (saves a label set); shows when last reviewed. `play-area-editor.tsx`: draggable polygon over
  the paused video (letterbox-aware), Save & re-detect / Cancel / Reset.
- Verified in the browser while signed in: nudged rally #1's end and restored it (edits
  recorded, effective rally back to 6.8-14.2 s), play-area editor showed the saved polygon
  aligned with the court; cancelling queued no job. The learned model's rally #1 (0:07-0:14)
  matches Balltime's ace rally.

### M7 — Highlight export (2026-11-26)
- API (`routers/highlights.py`): `POST /videos/{id}/highlights {rally_ids, padding_s, name}`
  freezes clip times (effective rallies, padded 0.5 s, merged when they touch),
  `GET /videos/{id}/highlights` (status, progress, presigned download with a friendly file
  name), `DELETE /highlights/{id}`. Exports expire after 30 days and count toward the storage cap.
- Worker (`highlight.py`, job kind `highlight`): cuts each clip from the 720p proxy with
  input seeking + re-encode (frame-accurate, CRF 22), joins with the concat demuxer, uploads,
  marks ready atomically; a job that fails for good marks the export failed.
- Web: "Highlight videos" panel on the match page (export all / starred, progress, download, delete).
- Verified: tests (cut + join duration = sum of clips, failure path); real export of the
  sample match: 47 clips, 9.9 min, 112 MB, cut 165 s + join 2 s on the dev laptop.
- Sign-in: Supabase's built-in mailer hit its hourly limit after repeated re-logins. Added
  password sign-in (toggle on /login) and "Set a password" on /account.

### Generalisation work (2026-11-26)
**State when handed off:** all code committed; the gated retrain (`2026-11-26.1`) was stopped
before it registered anything, so local production is still `2026-11-25.1` (trained on TFC only).

Second reference match: HJSC "Team Adam vs Big Chillin G2 S1" (16:13, different gym, no play
area marked). Balltime list read from their rally panel, cut aligned (45 rallies, all in order,
match >= 0.975). Reference files now live in `ml/references/` (`*.balltime.json` = rallies in
original-video seconds; `*_cut_times.txt` = Balltime's condensed-timeline list).
Source videos are NOT in git (owner's Downloads folder on the work laptop):
`Nov 15, 2026 TFC Asians Tourney ... [EiwsJWsGqwA].mp4` (+ ` (1).mp4` Balltime cut) and
`Oct 29, 2026 Team Adam vs Big Chillin at HJSC ... [3LDBrZ2Peeo].mp4` (+ ` balltime].mp4`).

Results (F1@tIoU0.5 vs Balltime, each test gym unseen in training):
| Recipe | TFC->HJSC | HJSC->TFC |
|---|---|---|
| production 2026-11-25.1 (TFC only) on HJSC | 0.00 (0 predicted; probs ~0) | — |
| global feature normalisation | 0.00 | 0.14 |
| **per-video feature normalisation** (adopted in code) | **0.59** (start/end err 1.0/1.2 s) | **0.73** (1.0/1.0 s) |

Code added: `ml/src/vball_ml/matches.py` (`score` / `import` / `retrain` with leave-one-match-
out and a promotion gate; production model records `trained_on`), `video_norm()` in
`rally_model.py` (bundles with `meta.input_norm == "per_video"` apply it at predict time), worker
saves `learned_features.npz` per analysis (`params.learned_features_key`) so retraining doesn't
re-extract.

**Owner's goal: ~95% F1 vs Balltime on unseen gyms.** Planned next experiments (score every one
leave-one-gym-out; log results here; avoid brute-force tuning on just two videos):
1. Consistent input: TFC features used a play-area crop, HJSC full frame. Re-extract TFC full
   frame (or always full frame) and compare.
2. Motion cues: add embedding differences (x_t - x_{t-1}) / short-window std.
3. Augmentation: feature noise, random temporal scaling, audio gain jitter.
4. Decoding robust to calibration shift: per-video adaptive threshold (e.g. quantile/Otsu)
   vs fixed 0.5 — generic, not fitted per video.
5. Capacity/regularisation: width, dropout, epochs.
Then run `python -m vball_ml.matches retrain --version <v>` (needs the local stack + both
videos' features in storage) and rebuild the worker. A third match from another gym is needed
to confirm any score near 95% isn't selection luck.

New-PC notes: local Docker data (database, storage, models, features) does NOT transfer. To
continue experiments on another PC: copy the 4 match videos, start the stack, re-ingest, and
features are recomputed (~15 min/match on CPU), or just continue other milestones (M8 deploy).

### Three-match model + honest agreement in the UI (2026-11-27)
Why HJSC showed no rallies: production was still `2026-11-25.1` (TFC only, global
normalisation), which predicts ~0 everywhere at HJSC. Fixed by shipping the cross-gym recipe.

- Third reference match (TFC "Dino vs Carlos Santos" T2 G1 S1, 46 rallies aligned) imported.
- `python -m vball_ml.train_production --version V --trained-on ID,ID,ID --register` trains the
  best recipe (motion cues, per-video norm, width 128, dropout 0.5, min gap 3 s) on all
  reference matches. The model file carries its recipe (`Bundle.prepare` / `decode_params`), so
  the worker runs it exactly as evaluated. Production is now **`2026-11-27.1`** (tfc, hjsc,
  tfc2); `2026-11-25.1` retired. All three videos re-analysed (47 / 45 / 46 rallies).
- Worker re-uses a previous run's `learned_features.npz` when extractor and play area match, so
  re-detecting with a new model takes seconds.
- Held-out results, 3 matches (F1@tIoU0.5 vs Balltime):

| Test | TFC Finals | HJSC | TFC Dino/Carlos | mean |
|---|---|---|---|---|
| leave-one-match-out | 0.96 | 0.81 | 1.00 | 0.921 |
| **leave-one-gym-out** | 0.91 | 0.81 | 0.94 | **0.884** |

- UI: each video page shows an "Agreement with Balltime" card (core `compare_to_reference`,
  API `RalliesOut.reference`), per-rally "not in Balltime" tags and missed-rally buttons. When the
  active model was trained on that video, the card leads with the held-out score
  (`metrics.held_out_by_video`, leave-one-gym-out preferred) and shows the in-sample agreement
  only as a footnote, so improvements seen in the UI are honest.
- Next levers toward 95% on unseen gyms: more matches from other gyms (biggest), a pretrained
  ball tracker (TrackNetV3, MIT) and/or player detector as extra features, judged
  leave-one-gym-out. HJSC's weakness is rally ends (median end error 1.4 s) and a few short
  rallies missed.
- Local stack: always start compose with `--env-file .env` (without it the API exits on an
  empty `supabase_url`).

### Fourth match, unseen-match test, M8 groundwork (2026-11-27/28)
**Reference labels without Balltime's rally list.** `ml/eval/segment_reference_cut.py ORIGINAL
CUT OUT.json` matches every 2 s of a Balltime cut to the original (fingerprints) and splits where
the time offset jumps. Balltime's export pads each rally (-0.4 s start, +2.8 s end vs its list):
measured on HJSC G2S1 (45/45), confirmed on TFC Dino/Carlos without retuning (46/46, F1 1.000,
median 0.2/0.6 s). So the owner only needs to send the original + Balltime's cut.

**Data:** 4th training match HJSC "Adam vs Big Chillin G2 S2" (50 rallies;
`data/features/hjsc2.full.npz`, made with the app's 720p proxy settings; not in the app).
Test-only match TFC "Dino vs Harry Xu Seeding G2 S2" (45 rallies, app video e3fd973f…), never
used for training. `vball_worker.dev_upload PATH` adds a local file to the app like a browser
upload.

| Model | Harry Xu (unseen) F1@0.5 vs Balltime |
|---|---|
| 2026-11-27.1 (3 matches) | 0.841 (43 rallies; offline score = app, so results are reproducible) |
| **2026-11-27.2 (4 matches, now production locally)** | **0.870** (47 rallies, 40/45 found) |

4-match leave-one-gym-out: mean 0.841 (tfc 0.93, hjsc 0.81, tfc2 0.96, hjsc2 0.67). Main error
on Harry Xu: back-to-back rallies merged (quick next serve). Next ML step: fix that
(between-rally gaps), judged leave-one-gym-out and on Harry Xu; more gyms remain the biggest
lever. Promotion rule used: train with `train_production --out`, score the unseen test match
offline, `matches publish` only if better.

**M8 deployed (2026-11-28):** web https://vball-ai-jtsd.vercel.app (Vercel project
`vball-ai-jtsd`, root apps/web), API https://vball-ai.vercel.app (Vercel project `vball-ai`, root
deploy/api; /readyz checks database + storage), Supabase DB migrated to 0005, B2 bucket
`vball-mz-2026` (us-east-005, CORS for the web URL; lifecycle must be "keep only the last
version"), GitHub Actions secrets set, rally model 2026-11-27.2 published to production.
Owner's settings live in git-ignored infra/production.env. Verified: API health/readiness,
CORS from the web origin, worker run connects to the DB and exits idle. Fixed on the way: worker
steps were skipped on dispatched runs ('' == '0' in Actions expressions).

**M8 (deploy) groundwork:** Modal ruled out (without a card it gives $1/month, not $30). Plan:
web + API on Vercel (`apps/web`, `deploy/api`), Supabase DB, Backblaze B2 storage, video
processing on GitHub Actions (`.github/workflows/worker.yml`, started by the API via
`repository_dispatch` + half-hourly safety net; `python -m vball_worker --until-idle`).
Worker runs on CPU (~15–20 min/match). Owner steps: docs/DEPLOY.md. The Vercel entry
(`deploy/api/api/index.py`) runs FastAPI startup on the first request if the runtime skips
lifespan (tested locally with lifespan off). Speed ideas for later: decode once for proxy +
embeddings; GPU if a free no-card option appears.

### Cloud run-through, parallel analysis, B2 download cap (2026-11-28) — RESUME HERE
**Working in the cloud:** phone upload → API → GitHub Worker (repository_dispatch) → ingest
6.1 min + analysis 9.8 min for a 19-min set on one 4-core runner → 45 rallies (model
2026-11-27.2). Manual rally editor removed from the video page (owner: the model does the
cutting).

**Parallel analysis (built, not yet measured):** `vball_worker.shards` + `.github/workflows/
embed.yml`; worker.yml sets `VBALL_EMBED_SHARDS=4` (worker embeds slice 1, 3 helper runners the
rest, missing slices redone locally). Slices join bit-for-bit (900/900 rows). First cloud test
failed (worker lacked `httpx`, fell back to heuristic; fixed), second was blocked by the B2 cap.
**Next session: check the second upload (video 3332a3c3…), queued for after the reset; its
analysis is the first real sharded run.** Read its Worker + Embed slice logs (`embed_sharded`,
`embed_joined` events) and compare with 9.8 min.

**Backblaze free tier = 1 GB/day downloads** (resets 00:00 GMT): one match costs ~0.7 GB
(original 533 MB to ingest + proxy 184 MB to analyse) before any watching, so ~1 match/day.
Storage 403s now defer the job to 00:05 GMT without using an attempt (`jobs.defer`). Owner
(2026-11-28): fine for now; would add a card later if it stays cheap and secure, possibly to
share privately with friends. Options next: (a) compute model inputs during ingest (saves the
184 MB proxy download; helpers add back ~140 MB), (b) card on B2 with a daily cap (~$0.01/GB),
(c) GPU via Modal with a card (setup in git history, commit 8749d4c). If friends get accounts:
security review of per-owner access first.

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
