# Deploying VBall (M8): free tiers, no credit card

| Piece | Runs on | Free allowance |
|---|---|---|
| Web app (Next.js, `apps/web`) | Vercel Hobby | plenty for one user |
| API (FastAPI, `deploy/api`) | Vercel Hobby (Python function) | plenty for one user |
| Sign-in + database | Supabase free (existing project) | 500 MB database |
| Video storage | Backblaze B2 | 10 GB stored, 1 GB/day downloads |
| Video processing | GitHub Actions (`.github/workflows/worker.yml`) | free for public repos, 4 CPU cores |

How it fits together: the browser talks to the API and uploads straight to B2. When the API
queues work (an upload, a re-analysis, a highlight export) it starts the **Worker** workflow on
GitHub, which processes everything waiting and exits (~15–20 min for a 17-minute match on CPU).
A half-hourly scheduled run picks up anything missed. Nothing runs while idle.

Known limits:
- Supabase pauses a free project after 7 days without use: open the app once a week, or
  un-pause it in the Supabase dashboard.
- GitHub disables scheduled workflows in a repo with no activity for 60 days (uploads still
  start the worker directly; re-enable under Actions → Worker if needed).
- The repo is public, so Worker logs are public. They contain only job ids and timings, never
  file names, links or keys (GitHub also masks secrets).
- B2's free download allowance is 1 GB/day: watching ~1 hour of 720p video a day.

## 1. Backblaze B2 (5 min)
1. https://www.backblaze.com/sign-up/cloud-storage → email + password (no card). Verify email.
2. **B2 Cloud Storage → Buckets → Create a Bucket**: name `vball-` + something unique (e.g.
   `vball-mz-2026`), **Private**, default encryption off, object lock off. Note the
   **Endpoint** shown on the bucket card (e.g. `s3.us-west-004.backblazeb2.com`).
3. **Application Keys → Add a New Application Key**: name `vball`, bucket = that bucket,
   access **Read and Write**. Copy **keyID** and **applicationKey** (shown only once).

## 2. Supabase (3 min)
1. https://supabase.com/dashboard → project **hkfzpbzivnnwspckzjbi** → **Connect** (top bar) →
   copy the **Session pooler** connection string. Change `postgresql://` to
   `postgresql+psycopg://` and put your database password in place of `[YOUR-PASSWORD]`
   (forgot it? **Project Settings → Database → Reset database password**).
2. Later, after step 5: **Authentication → URL Configuration** → Site URL = the web app's
   Vercel URL; Redirect URLs → add `https://<web-app>.vercel.app/**`.

## 3. Production settings file (local, never committed)
Copy `infra/production.env.example` to `infra/production.env` (git-ignored) and fill in the
values from steps 1–2. In PowerShell from the repo root, load it into this terminal only and
create the cloud database tables:
```
Get-Content infra/production.env | % { if ($_ -match '^(VBALL_\w+)=(.*)$') { Set-Item "env:$($matches[1])" $matches[2] } }
uv run python -m alembic upgrade head
```
(Open a new terminal afterwards to get back to the local setup.)

## 4. GitHub (5 min)
1. **Worker secrets**: https://github.com/martinzukowski/vball-ai/settings/secrets/actions →
   **New repository secret**, one per line of `infra/production.env` that the worker uses:
   `VBALL_DATABASE_URL`, `VBALL_SUPABASE_URL`, `VBALL_S3_ENDPOINT_URL`, `VBALL_S3_REGION`,
   `VBALL_S3_BUCKET`, `VBALL_S3_ACCESS_KEY_ID`, `VBALL_S3_SECRET_ACCESS_KEY`.
2. **Token that lets the API start the worker**: https://github.com/settings/personal-access-tokens/new
   → name `vball-worker-dispatch`, expiration 1 year, **Only select repositories** →
   `vball-ai`, **Repository permissions → Contents: Read and write** (needed for
   `repository_dispatch`; nothing else) → **Generate token**. Copy it into
   `VBALL_WORKER_DISPATCH_TOKEN` in `infra/production.env`.

## 5. Vercel: two projects from the same repo (10 min)
**API** — https://vercel.com/new → import `martinzukowski/vball-ai` → **Root Directory**
`deploy/api` (Framework preset: Other) → **Environment Variables**: every `VBALL_*` line from
`infra/production.env` (paste the whole file into the first field; Vercel splits it) → Deploy.
Note the URL, e.g. `https://vball-api-xxx.vercel.app`; `/healthz` should say `ok`.

**Web** — https://vercel.com/new → import the same repo again → **Root Directory** `apps/web` →
Environment Variables: `NEXT_PUBLIC_API_URL` = the API URL, `NEXT_PUBLIC_SUPABASE_URL` and
`NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` (same values as in the local `apps/web/.env.local`) →
Deploy.

Then: put the web URL in `VBALL_CORS_ORIGINS` (API project → Settings → Environment Variables →
edit → Redeploy), finish step 2.2, and allow browser uploads to the bucket (terminal with
production.env loaded): `uv run python infra/storage_cors.py https://<web-app>.vercel.app`

## 6. Production rally model
In a terminal with production.env loaded:
`uv run python -m vball_ml.matches publish data/model_<v>.pt --version <v>` uploads the model
file to B2 and registers it as production in the cloud database (the worker checks its sha256).
Until a model is published, the worker falls back to the heuristic detector.

## 7. Check
Sign in on your phone, upload a short clip, watch **Actions → Worker** start within seconds,
and the video page show rallies when it finishes.
