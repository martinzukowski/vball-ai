# Deploying VBall (M8): free tiers, no credit card

| Piece | Runs on | Free allowance | Card? |
|---|---|---|---|
| Web app (Next.js) | Vercel Hobby | plenty for one user | no |
| Sign-in + database | Supabase free (existing project) | 500 MB database | no |
| Video storage | Backblaze B2 | 10 GB stored, 1 GB/day download | no |
| API + workers | Modal Starter (`infra/modal/vball_app.py`) | $30 of compute / month | **check at sign-up** |

How it fits together: the browser talks to the API (Modal HTTPS URL) and uploads straight to
B2. Every minute a tiny Modal function checks the job queue in Supabase; when a video is
waiting it starts a CPU worker (ingest, exports) or a T4 GPU worker (rally model). Nothing runs
while idle. A 17-minute match costs roughly $0.10–0.20 of the $30.

Known limits: Supabase pauses a free project after 7 days without use (open the app once a
week, or un-pause in the dashboard). Modal's free $30 resets monthly; at that price the app
handles ~150+ matches a month.

## 1. Modal (5 min). Stop here if it asks for a card
1. Go to https://modal.com and click **Sign up** → **Continue with GitHub** (martinzukowski).
2. If any screen asks for a credit card, **stop** and tell Claude: we switch the workers to the
   fallback (GitHub Actions) instead.
3. In a terminal in the repo: `uvx modal setup`. A browser tab opens; click **Approve**.

## 2. Backblaze B2 (5 min)
1. https://www.backblaze.com/sign-up/cloud-storage → email + password (no card). Verify email.
2. **B2 Cloud Storage → Buckets → Create a Bucket**: name `vball-` + something unique (e.g.
   `vball-mz-2026`), **Private**, encryption off, object lock off. Note the **Endpoint**
   shown on the bucket (e.g. `s3.us-west-004.backblazeb2.com`).
3. **Application Keys → Add a New Application Key**: name `vball`, allow access to that bucket
   only, Read and Write. Copy **keyID** and **applicationKey** (shown once).

## 3. Supabase database URL (2 min)
1. https://supabase.com/dashboard → project **hkfzpbzivnnwspckzjbi** → **Connect** (top bar).
2. Copy the **Session pooler** connection string. Change `postgresql://` to
   `postgresql+psycopg://` and put in your database password.
3. **Authentication → URL Configuration**: add the Vercel URL (step 5) as Site URL and to
   Redirect URLs as `https://<your-app>.vercel.app/**`.

## 4. Fill in production settings (local file, never committed)
Copy `infra/production.env.example` to `infra/production.env` (git-ignored) and fill in the
values from steps 2–3. Then, in PowerShell from the repo root:
```
Get-Content infra/production.env | % { if ($_ -match '^(VBALL_\w+)=(.*)$') { Set-Item "env:$($matches[1])" $matches[2] } }
uv run python -m alembic upgrade head
uvx modal secret create vball --from-dotenv infra/production.env
uvx modal deploy infra/modal/vball_app.py
```
(The first line loads production.env into this terminal only; open a new terminal to get back
to the local setup.)
The deploy prints the API URL (`https://<workspace>--vball-api.modal.run`).

## 5. Vercel (5 min)
1. https://vercel.com/new → import **martinzukowski/vball-ai** → Root Directory `apps/web`.
2. Environment variables: `NEXT_PUBLIC_API_URL` = the Modal API URL,
   `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` (same as local `.env`).
3. **Deploy**. Then put the Vercel URL into `VBALL_CORS_ORIGINS` in `infra/production.env`,
   re-run the `modal secret create ... --force` and `modal deploy` lines, and allow uploads from
   the site: `uv run python infra/storage_cors.py https://<your-app>.vercel.app`
   (with the B2 values from production.env in the environment).

## 6. Production rally model
In a terminal with production.env loaded (step 4):
`uv run python -m vball_ml.matches publish data/model_<v>.pt --version <v>` uploads the model
file to B2 and registers it as production in the cloud database (the worker checks its sha256).
Until a model is published, the cloud worker falls back to the heuristic detector.
