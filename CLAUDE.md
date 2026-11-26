# VBall — working agreement for Claude sessions

Read `docs/PROGRESS.md` first (status, decisions, "RESUME HERE" section), then
`ARCHITECTURE.md` / `IMPLEMENTATION_PLAN.md` as needed.

## Owner's constraints
- **One user** (the owner). No multi-tenant scaling work.
- **Free tiers only, no credit cards.** Storage: Backblaze B2 (not Cloudflare R2, which needs a
  card). Vercel account already exists (martinzukowski04@gmail.com). Check any other service for
  card requirements before asking the owner to sign up.
- Goal: a polished service to quickly cut volleyball videos into rallies; stats later.

## How to build
- Work milestone by milestone autonomously; stop only when the owner must sign in / create an
  account / provide footage, and then give exact click-by-click steps.
- Commit and push to `main` after each milestone; keep `docs/PROGRESS.md` updated (what was done,
  how it was verified, decisions, next steps).
- Never add AI attribution to pull requests.

## ML direction (owner, 2026-11-25)
- Rally (and later action) detection must be **learned ML/CV that generalises**, not
  hand-tuned thresholds; `heuristic-v1` is only a baseline/fallback.
- Every change is judged on **held-out matches from gyms not used in training**
  (leave-one-match-out); no silent promotions — `python -m vball_ml.matches retrain` gates
  promotion. Don't tune to a single video.
- The owner prefers not to review clips: labels come from Balltime (owner uploads the same match
  there; Balltime exports a condensed cut; `ml/eval/align_reference_cut.py` maps it back).
  Balltime output is a reference, never shown as our own work.
- Current target: ~95% F1@0.5 vs Balltime on unseen gyms; confirm any result with a third gym.

## Environment quirks (owner's Windows laptop)
- Running `.venv\Scripts\*.exe` is blocked: use `uv run python -m pytest|mypy|alembic`.
- Port 3000 is taken; web dev server runs on 3100.
- Supabase's built-in mailer allows ~2 emails/hour: prefer password sign-in (Account page).
- Restarting the web preview signs the in-app browser out; avoid needless restarts.
