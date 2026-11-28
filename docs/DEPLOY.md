# Deploying VBall (M8)

| Piece | Runs on | Cost |
|---|---|---|
| Web app (Next.js, `apps/web`) | Vercel Hobby, project `vball-ai-jtsd` | free |
| API (FastAPI, `deploy/api`) | Vercel Hobby, project `vball-ai` | free |
| Sign-in + database | Supabase free (project hkfzpbzivnnwspckzjbi) | free (500 MB) |
| Video storage | Backblaze B2 bucket `vball-mz-2026` | 10 GB + 1 GB/day downloads free; beyond that ~$0.01/GB under a daily cap |
| Video processing | Modal (`infra/modal/vball_app.py`): 4-core CPU + T4 GPU | $30/month credit; a match costs a few cents |
| Backup processing | GitHub Actions (`.github/workflows/worker.yml`), manual only | free |

How it fits together: the browser talks to the API and uploads straight to B2. After queuing
work the API calls Modal's `wake` endpoint (bearer token), which starts a CPU worker for the
playback copy and exports, then a GPU worker for rally analysis. A 30-minute `sweep` catches
anything missed (for example a job waiting for B2's daily cap to reset). Nothing runs while
idle. Measured on a 20-minute set: playback copy 2 min 15 s, rally analysis 61 s.

All production settings live in **`infra/production.env`** (git-ignored; template
`infra/production.env.example`). The same values go to three places: the Vercel API project's
environment variables, the Modal secret `vball`, and GitHub's Actions secrets (backup worker).

## Security
- **Invite-only.** Supabase sign-up is turned off (Authentication → Sign In / Providers →
  *Allow new users to sign up* off), and the API also checks `VBALL_ALLOWED_EMAILS`: a valid
  account whose email isn't listed gets 403 and no data.
- **Per-user data.** Every API route checks the video belongs to the signed-in user (others get
  404); tests cover it. Supabase's public REST interface has no access to any table, the B2
  bucket is private (only short-lived signed links), and API docs are off in production.
- **Secrets** are only in `infra/production.env` locally and in Vercel / Modal / GitHub secret
  stores. Modal's `wake` endpoint rejects calls without the token.
- **Shared storage cap.** The 9 GB storage cap (`VBALL_STORAGE_CAP_BYTES`) is for the whole
  bucket, i.e. shared by everyone invited.

## Inviting a friend
1. Supabase → **Authentication → Users → Add user → Create new user**: their email and a
   temporary password, **Auto Confirm User** on. (Or *Send invitation*; the built-in mailer
   sends ~2 emails/hour.)
2. Add their email to `VBALL_ALLOWED_EMAILS` (comma-separated): Vercel → project `vball-ai` →
   Settings → Environment Variables → edit → **Deployments → Redeploy**. Also update
   `infra/production.env` so it stays the record.
3. Send them https://vball-ai-jtsd.vercel.app; they sign in with **Sign in with a password
   instead** and can change the password on the Account page.

## Redeploying after code changes
- Web and API: automatic on every push to `main` (Vercel).
- Modal workers (after changes to the worker, `ml/`, or `vball_core`), from the repo root:
  ```
  $env:PYTHONUTF8 = "1"; uv run --with modal modal deploy infra/modal/vball_app.py
  ```
  (`PYTHONUTF8` avoids a Windows console encoding error in the build log.) After `uv.lock`
  changes run `sh infra/modal/export_requirements.sh` first.
- Database changes: load production settings into the terminal, then `uv run python -m
  alembic upgrade head`:
  ```
  Get-Content infra/production.env | % { if ($_ -match '^(VBALL_\w+)=(.*)$') { Set-Item "env:$($matches[1])" $matches[2] } }
  ```
- Settings changes: edit `infra/production.env`, then
  `uvx --with python-dotenv modal secret create vball --from-dotenv infra/production.env --force`,
  the matching Vercel variable (+ Redeploy), and `gh secret set -f infra/production.env`.
- New rally model: with production settings loaded,
  `uv run python -m vball_ml.matches publish data/model_<v>.pt --version <v>`.

## Setting it up from scratch (done 2026-11-28; kept for reference)
1. **Backblaze B2**: sign up; Buckets → Create (private; lifecycle *Keep only the last version*);
   Application Keys → key for that bucket, Read and Write. Billing: add a card, then
   **Caps & Alerts** → daily download cap (e.g. $0.50/day).
2. **Supabase**: Connect → Direct → **Session pooler** URI, as `postgresql+psycopg://…`.
   Authentication → URL Configuration: Site URL = web URL; Redirect URLs = web URL `/**` and
   `http://localhost:3100/**`. Turn sign-up off (see Security).
3. Fill `infra/production.env`; run migrations (above).
4. **Modal**: sign up with GitHub, add a payment method and a workspace budget; `uvx modal
   setup`; create the secret (above, including a random `VBALL_WORKER_WAKE_TOKEN`); deploy.
5. **GitHub** (backup worker): `gh secret set -f infra/production.env --repo
   martinzukowski/vball-ai`.
6. **Vercel**: import the repo twice. API: root `deploy/api`, preset Other, all `VBALL_*`
   variables. Web: root `apps/web`, `NEXT_PUBLIC_API_URL` (API URL),
   `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`,
   `NEXT_PUBLIC_AUTH_GOOGLE_ENABLED=false`. Then `VBALL_CORS_ORIGINS` = web URL (Redeploy) and
   `uv run python infra/storage_cors.py <web URL>` (production settings loaded).
7. Publish the rally model (above) and upload a test video.
