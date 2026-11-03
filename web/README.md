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

`.env.example` sets `ALLOW_SIGNUP=1` so the login page offers "Create account" in dev. Without it sign-up is closed
(see Production).

API side (repo root), pointing the API at this app's JWKS:

```bash
VREVIEW_AUTH_JWKS_URL=http://localhost:53000/api/auth/jwks \
VREVIEW_AUTH_ISSUER=http://localhost:53000 VREVIEW_AUTH_AUDIENCE=http://localhost:53000 \
uv run vreview-api                 # :8000; set NEXT_PUBLIC_VREVIEW_API_URL=http://localhost:8000
```

`NEXT_PUBLIC_*` values are inlined at build/dev start, so restart `pnpm dev` after changing them.

## Production (Docker, Coolify)

`Dockerfile` builds a Next.js `output: "standalone"` image (node 24, non-root `node` user, port 3000, `HOSTNAME=0.0.0.0`).
The build context is this directory.

```bash
docker build --build-arg NEXT_PUBLIC_VREVIEW_API_URL=https://api.example.com -t vreview-web .
```

| Name | Kind | Secret | Example |
| --- | --- | --- | --- |
| `NEXT_PUBLIC_VREVIEW_API_URL` | build arg (inlined in the browser bundle, rebuild to change) | no | `https://api.example.com` |
| `BETTER_AUTH_URL` | runtime env | no | `https://vreview.example.com` |
| `BETTER_AUTH_SECRET` | runtime env | yes | `openssl rand -base64 32` |
| `DATABASE_URL` | runtime env | yes | `postgres://vreview:***@postgres:5432/vreview_auth` |
| `ALLOW_SIGNUP` | runtime env, optional | no | unset (closed). `1` opens sign-up, dev only |
| `BETTER_AUTH_TRUSTED_ORIGINS` | runtime env, optional | no | `https://www.vreview.example.com` (comma separated) |

The image fixes `NODE_ENV=production`, `PORT=3000`, `HOSTNAME=0.0.0.0`, `NEXT_PUBLIC_VREVIEW_MOCK=0` and
`KEEP_ALIVE_TIMEOUT=95000` (above Traefik's 90 s backend idle timeout). The build fails when
`NEXT_PUBLIC_VREVIEW_API_URL` is empty. `next build` prints a few "default secret" `BetterAuthError` lines; that is
expected (secrets exist only at runtime) and does not fail the build.

- **Public URL.** `BETTER_AUTH_URL` is the exact origin users open (no trailing slash). It is also the JWT `iss` and
  `aud`, so the API needs `VREVIEW_AUTH_ISSUER` and `VREVIEW_AUTH_AUDIENCE` set to the same string. The API can read the
  keys from an internal URL, for example `VREVIEW_AUTH_JWKS_URL=http://web:3000/api/auth/jwks`. The API must also allow
  the web origin in its CORS settings, because the browser calls it directly.
- **Behind a proxy.** With an `https://` `BETTER_AUTH_URL`, cookies are `Secure` (`__Secure-` prefix) and
  `X-Forwarded-Host`/`X-Forwarded-Proto` are trusted, so the app must only be reachable through the proxy (do not
  publish port 3000). Rate limiting keys on `X-Forwarded-For`, which Traefik sets to the client address; a second proxy
  in front of Traefik needs `trustedProxies` in `src/lib/auth.ts`. With `http://` (local runs) none of this applies.
- **Health.** `GET /api/health` returns `{"status":"ok"}` without authentication. It checks that the server is up, not
  the database, so a database outage does not take the login page out of the proxy rotation. The image has a Docker
  `HEALTHCHECK` on it.
- **Migrations.** `scripts/docker-entrypoint.sh` runs `scripts/migrate.mjs` before the server starts, on every start.
  It uses better-auth's `getMigrations`, takes a Postgres advisory lock so concurrent starts do not race, waits up to
  30 s for a booting database, and does nothing when the schema is current. A failure stops the container before it
  serves traffic. On an empty database better-auth first logs one "Database schema mismatch" error, then the
  migration creates the tables. The database itself (`vreview_auth`) must already exist.
- **Sign-up is closed** unless `ALLOW_SIGNUP=1`: `POST /api/auth/sign-up/email` returns 400 and the login page says
  sign-up is closed instead of showing the form. The flag is read at request time, no rebuild needed.

### Users

Accounts are created from the command line. This works while sign-up is closed because it writes through
better-auth's own database layer with its password hashing (same result as the sign-up endpoint).

```bash
# in the production container (npm; pnpm is not installed in the runtime image)
docker exec <web-container> npm run user:create -- --email you@example.com --password 'a long passphrase' --name You
docker exec <web-container> npm run user:set-password -- --email you@example.com --password 'a new passphrase'

# in a dev checkout (reads .env.local; variables already in the environment win over it)
pnpm user:create -- --email you@example.com --password 'a long passphrase' [--name You]
pnpm user:set-password -- --email you@example.com --password 'a new passphrase'
```

- `user:create` is idempotent: an existing user (matched by lower-cased email) is left unchanged and the command exits
  0. It also finishes the credential account of a user that exists without one. `--name` defaults to the part of the
  email before `@`.
- `user:set-password` fails (exit 1) for an unknown email, otherwise replaces the password and signs out that user's
  existing sessions, like better-auth's password reset.
- Passwords must be 8 to 128 characters. Exit codes: 0 done, 1 failure, 2 usage error.
- Needs `DATABASE_URL` and `BETTER_AUTH_SECRET` (and `BETTER_AUTH_URL` in production). The password is visible in the
  process list and in the command echo that npm/pnpm print, so use a temporary one and change it with
  `user:set-password`, or add `--silent` after `npm run` to hide the echo.

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
pnpm verify:auth                   # sign up/in a test user, verify a JWT against /api/auth/jwks (iss/aud); sign-up needs ALLOW_SIGNUP=1, else set TEST_EMAIL/TEST_PASSWORD of an existing user
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
