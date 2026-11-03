# vreview web

Next.js 16 (App Router) + better-auth UI for the vreview API. Contract: `../docs/triage-and-ui-contract.md`.

## Run

```bash
cp .env.example .env.local        # fill BETTER_AUTH_SECRET (openssl rand -base64 32)
docker exec vreview-postgres-1 psql -U vreview -d postgres -c "CREATE DATABASE vreview_auth OWNER vreview"  # once
pnpm install
pnpm auth:migrate                  # better-auth tables (user, session, account, verification, jwks)
pnpm dev                           # http://localhost:53000
```

API side (repo root), pointing the API at this app's JWKS:

```bash
VREVIEW_AUTH_JWKS_URL=http://localhost:53000/api/auth/jwks \
VREVIEW_AUTH_ISSUER=http://localhost:53000 VREVIEW_AUTH_AUDIENCE=http://localhost:53000 \
uv run vreview-api                 # :8000; set NEXT_PUBLIC_VREVIEW_API_URL=http://localhost:8000
```

`NEXT_PUBLIC_*` values are inlined at build/dev start, so restart `pnpm dev` after changing them.

## Mock mode

`NEXT_PUBLIC_VREVIEW_MOCK=1` swaps the API client for `src/lib/api/mock.ts`: reviews come from the real
es-v1 result (`src/mocks/es-v1-result.json`) with triage computed by `src/lib/triage.ts` (a port of the
contract rules; it gives the same counts and threads as `vreview.triage` on that fixture). Uploads are
simulated (part 2 fails once to exercise retry), and new reviews move through the stages every 4 s.
`VREVIEW_MOCK_ORIGINAL_PATH` / `VREVIEW_MOCK_CANDIDATE_PATH` are streamed to the players by
`/api/mock-media/*`. Sign-in is always real.

## Checks

```bash
pnpm lint && pnpm build
pnpm verify:auth                   # sign up/in a test user, verify a JWT against /api/auth/jwks (iss/aud)
pnpm dev:mock                      # in another shell: mock mode on :53000 (the process env overrides .env.local)
pnpm screenshots                   # mock mode: screenshots/*.png, also checks overlay, 1024 px overflow, cancel/abort
RUN=<succeeded run id> BIG=big.mp4 node scripts/real-checks.mjs   # real API: overlay, feedback survives reload, cancel -> abort
CANDIDATE=dub.mp4 ORIGINAL=orig.mp4 node scripts/real-api-check.mjs   # real API: upload through the UI (creates a run)
```

Screenshots are viewport shots only and the script fails on any image over 2000 px (long pages are
captured as several scrolled shots), because larger images are rejected when an agent views them.

Playwright screenshots must pass `caret: "initial"`. The default (`"hide"`) injects `caret-color: transparent`
into every `<input>`; if React has not hydrated yet it reports a hydration mismatch and the Next.js dev overlay
shows a "1 Issue" badge. The app itself renders without dev-overlay issues.

Feedback (Accept / False positive / Comment) is saved with `POST /v1/reviews/{run_id}/issues/{issue_id}/feedback`
and loaded on the review page from `GET /v1/reviews/{run_id}/feedback` (full history; `?latest=true` keeps one
row per issue, so a comment would hide the verdict). Cancelling an upload calls `POST /v1/uploads/{asset_id}/abort`.
