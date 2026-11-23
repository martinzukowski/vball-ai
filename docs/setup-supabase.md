# Supabase setup (auth + database)

You need to do this once; it can't be automated from the repo because it requires your account.

## 1. Create the project

1. Sign in at https://supabase.com/dashboard and create a new project (Free plan is fine for
   development). Pick the region closest to you. Save the database password in your password
   manager. **Don't paste it into chat or commit it.**
2. Wait for provisioning to finish.

## 2. Check JWT signing keys (required)

Go to **Project Settings → JWT Keys** (wording may vary slightly). The API only accepts tokens
signed with **asymmetric keys** (ECC P-256 / ES256 or RSA). If the project still uses the legacy
HS256 secret, migrate to the new signing keys there. Check that
`https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json` returns at least one key.

## 3. Auth URL configuration

**Authentication → URL Configuration**

- Site URL: `http://localhost:3000` for now (the Vercel URL later, in M8).
- Redirect URLs: add `http://localhost:3000/auth/callback`, and later
  `https://<your-vercel-domain>/auth/callback`.

**Authentication → Sign In / Providers**

- Email: enabled (magic links). The built-in email sender is rate-limited and meant for
  development only. Configure custom SMTP before real use.
- Google (optional): needs an OAuth client from Google Cloud Console. Set
  `NEXT_PUBLIC_AUTH_GOOGLE_ENABLED=true` in the web app once it's configured.

## 4. Local configuration

Copy values from **Project Settings → API** (URL, publishable key):

`.env` (repo root):

```
VBALL_SUPABASE_URL=https://<project-ref>.supabase.co
```

`apps/web/.env.local`:

```
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_SUPABASE_URL=https://<project-ref>.supabase.co
NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=sb_publishable_...
```

Then restart the stack (`docker compose -f infra/docker/compose.yaml up -d --build`) and the
web app (`pnpm web:dev`), open http://localhost:3000, sign in, and the Account page should show
your user id and quotas fetched from the API.

## 5. Apply migrations to Supabase (when you want the hosted DB)

Use the **session pooler** connection string from **Connect** (port 5432), with the driver
prefix changed to `postgresql+psycopg://`, and run locally:

```
VBALL_DATABASE_URL='postgresql+psycopg://postgres.<ref>:<password>@<pooler-host>:5432/postgres' \
  uv run python -m alembic upgrade head
```

Keep that URL in your shell or a local `.env`, never in the repository.

## Testing on your phone (local network)

Magic links must be opened in the same browser that requested them. To try it on a phone before
deployment, run the web app with `pnpm --filter @vball/web dev -H 0.0.0.0`, use your PC's LAN IP
(e.g. `http://192.168.1.20:3000`) as an extra Redirect URL in Supabase, set
`NEXT_PUBLIC_API_URL=http://<lan-ip>:8000` and add `http://<lan-ip>:3000` to
`VBALL_CORS_ORIGINS`. The real phone test happens after deployment (M8).
