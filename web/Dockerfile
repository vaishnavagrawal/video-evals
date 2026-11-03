# syntax=docker/dockerfile:1
#
# vreview web (Next.js + better-auth). Build context: this directory.
#
#   docker build --build-arg NEXT_PUBLIC_VREVIEW_API_URL=https://api.example.com -t vreview-web .
#
# NEXT_PUBLIC_VREVIEW_API_URL is inlined into the browser bundle at build time, so it is a build arg and
# changing it needs a rebuild. Everything else is read at runtime (see .env.example and README "Production").
#
# Create the first user inside a running container (npm, not pnpm, see the runtime stage):
#   docker exec <container> npm run user:create -- --email you@example.com --password '...'

ARG NODE_VERSION=24

# ---- base: node + pnpm (version from package.json "packageManager"). pnpm is a native binary that corepack downloads
# on first run, so run it once here and the deps/prod-deps/build stages inherit it instead of each downloading it.
# `pnpm --version` writes a pnpm-lock.yaml next to package.json (package manager resolution); remove it again.
FROM node:${NODE_VERSION}-slim AS base
ENV COREPACK_HOME=/opt/corepack \
    COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY package.json ./
RUN corepack enable && corepack install && pnpm --version && rm -f pnpm-lock.yaml

# ---- deps: everything needed to build (dev dependencies included)
FROM base AS deps
COPY pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,id=vreview-web-pnpm-store,target=/pnpm-store \
    pnpm install --frozen-lockfile --store-dir /pnpm-store

# ---- prod-deps: production dependencies only. This becomes the runtime node_modules, so the operator scripts
# (scripts/migrate.mjs, scripts/user.mjs) run against the same better-auth and pg versions as the app.
FROM base AS prod-deps
COPY pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,id=vreview-web-pnpm-store,target=/pnpm-store \
    pnpm install --frozen-lockfile --prod --store-dir /pnpm-store

# ---- build: next build -> .next/standalone
FROM base AS build
ARG NEXT_PUBLIC_VREVIEW_API_URL
# Mock mode is dev-only. Fixed here (not a build arg) so no production bundle can ship with it on.
ENV NEXT_PUBLIC_VREVIEW_API_URL=${NEXT_PUBLIC_VREVIEW_API_URL} \
    NEXT_PUBLIC_VREVIEW_MOCK=0
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Without the arg the client would silently fall back to http://localhost:58000.
RUN test -n "${NEXT_PUBLIC_VREVIEW_API_URL}" || { echo "error: build arg NEXT_PUBLIC_VREVIEW_API_URL is required" >&2; exit 1; }
# next build prints a few "default secret" BetterAuthError lines while collecting page data: the build has no
# BETTER_AUTH_SECRET by design (secrets are runtime only). They are expected and do not fail the build.
RUN pnpm build

# ---- runtime: plain node, no pnpm/corepack (pnpm 12 wants to download itself and write a lockfile into the project
# directory at run time). Operator commands run through npm, which ships with node: `npm run user:create -- ...`.
FROM node:${NODE_VERSION}-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    NEXT_TELEMETRY_DISABLED=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NEXT_PUBLIC_VREVIEW_MOCK=0 \
    # Longer than Traefik's 90 s idle timeout toward backends, so the proxy never reuses a connection node just closed.
    KEEP_ALIVE_TIMEOUT=95000

# Code stays root-owned and read-only to the app user; only the Next.js cache directory is writable.
# server.js and .next come from the standalone trace; its bundled node_modules is replaced by the full production
# install below (the CLI scripts need better-auth, which Turbopack bundles into the server chunks instead of tracing it).
COPY --from=build /app/.next/standalone/server.js /app/.next/standalone/package.json ./
COPY --from=build /app/.next/standalone/.next ./.next
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
COPY --from=prod-deps /app/node_modules ./node_modules
# Operator scripts load the app's own auth config (type stripping), so ship the source of that one file.
COPY --from=build /app/src/lib/auth.ts ./src/lib/auth.ts
COPY scripts/auth-env.mjs scripts/migrate.mjs scripts/user.mjs scripts/docker-entrypoint.sh ./scripts/
# package.json (copied from the standalone output, identical to ours) provides the npm script names.
RUN mkdir -p .next/cache && chown node:node .next/cache

USER node
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/api/health',{signal:AbortSignal.timeout(4000)}).then(r=>process.exit(r.ok?0:1),()=>process.exit(1))"]

# Applies auth migrations (safe on every start), then execs the server so it is PID 1 and gets SIGTERM.
CMD ["sh", "scripts/docker-entrypoint.sh"]
