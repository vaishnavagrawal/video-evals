# vreview on Coolify

`docker-compose.yml` here is for Coolify's **Docker Compose** build pack, deployed from this repository. It is separate from `deploy/docker-compose.yml`, which is the local development stack and does not change.

## What runs

| Service | Image | Public | Purpose |
|---|---|---|---|
| `postgres` | `deploy/coolify/Dockerfile.postgres` (`pgvector/pgvector:0.8.7-pg16` plus the init script) | no | Databases `vreview` (app), `hatchet`, `vreview_auth` (web logins) |
| `minio` | `coollabsio/minio` | yes, `:9000` | S3 storage. Browsers PUT upload parts straight to presigned URLs on this host |
| `minio-init` | `coollabsio/minio` | no | One-shot: creates the private bucket `vreview` |
| `hatchet` | `hatchet-lite:v0.114.2` | no | Workflow engine (dashboard `:8888`, gRPC `:7077`, both internal) |
| `api` | repo `Dockerfile` | yes, `:8000` | FastAPI and MCP. Runs `alembic upgrade head` on every start |
| `worker` | repo `Dockerfile` | no | Hatchet worker: 2 slots, 6 GB RAM, 4 CPUs, 120 s to finish on stop |
| `web` | `web/Dockerfile` | yes, `:3000` | Next.js app and better-auth (issues the JWTs the API verifies) |

No service publishes a host port. Coolify's proxy routes to the three services that declare a `SERVICE_FQDN_*` variable.

## Coolify settings

| Setting | Value |
|---|---|
| Build pack | Docker Compose |
| Base Directory | `/` (the compose file's relative `build:` contexts `.`, `./web` and `./deploy/coolify` start here) |
| Docker Compose Location | `/deploy/coolify/docker-compose.yml` |
| Domains | one per public service, all `https://`: `api` as `https://api.example.com:8000`, `web` as `https://app.example.com:3000`, `minio` as `https://s3.example.com:9000` |
| Compose deployment | normal (not "Raw") |

Use `https` for all three domains. The web page is served over https, so the browser blocks uploads to an `http` MinIO host. "Preserve Repository During Deployment" is not needed: no file from the checkout is bind-mounted.

## Variables

Open the resource's **Environment Variables** after the first "Load compose" and fill in the ones marked "you".

| Variable | Supplied by | Secret | Notes / example |
|---|---|---|---|
| `SERVICE_FQDN_API_8000`, `SERVICE_URL_API` | Coolify, from the `api` domain | no | `https://api.example.com`. Used as `VREVIEW_PUBLIC_BASE_URL` and as the web build argument `NEXT_PUBLIC_VREVIEW_API_URL` |
| `SERVICE_FQDN_WEB_3000`, `SERVICE_URL_WEB` | Coolify, from the `web` domain | no | `https://app.example.com`. Becomes `BETTER_AUTH_URL`, the JWT issuer and audience, the API CORS origin, and the only CORS origin MinIO allows |
| `SERVICE_FQDN_MINIO_9000`, `SERVICE_URL_MINIO` | Coolify, from the `minio` domain | no | `https://s3.example.com`. Host used in presigned upload and download URLs |
| `SERVICE_USER_POSTGRES` | Coolify (generated) | yes | Postgres superuser; also used by api, worker, hatchet and web |
| `SERVICE_PASSWORD_POSTGRES` | Coolify (generated) | yes | |
| `SERVICE_USER_MINIO` | Coolify (generated) | yes | MinIO root user; the API and worker use it as their S3 access key |
| `SERVICE_PASSWORD_MINIO` | Coolify (generated) | yes | MinIO root password / S3 secret key |
| `SERVICE_PASSWORD_64_WEBHOOK` | Coolify (generated) | yes | `VREVIEW_WEBHOOK_SECRET`, HMAC key for outgoing webhooks. Receivers need it to verify `X-VReview-Signature` |
| `SERVICE_PASSWORD_64_BETTERAUTH` | Coolify (generated) | yes | `BETTER_AUTH_SECRET`. Also encrypts the JWKS private keys: changing it invalidates stored signing keys |
| `SERVICE_LOWERCASEUSER_HATCHETADMIN` | Coolify (generated) | no | Hatchet dashboard login is `<this value>@vreview.local` |
| `SERVICE_PASSWORD_HATCHETADMIN` | Coolify (generated) | yes | Hatchet dashboard password is `Aa1-<this value>`. Only applied when Hatchet first seeds its database |
| `OMNIROUTE_API_KEY` | **you** (required) | yes | LLM gateway key for the judge, speech and triage text |
| `OMNIROUTE_BASE_URL` | **you** (required) | no | e.g. `https://llm.example.com/v1` |
| `HATCHET_CLIENT_TOKEN` | **you**, after the first deploy | yes | See the runbook. Empty until then |
| `DUBEVAL_LLM_API_KEY` | you (optional) | yes | Overrides `OMNIROUTE_API_KEY` for the judge |
| `DUBEVAL_LLM_BASE_URL` | you (optional) | no | Overrides `OMNIROUTE_BASE_URL` for the judge |
| `DUBEVAL_LLM_MODEL` | you (optional) | no | Passed through to the worker |
| `VREVIEW_TRIAGE_LLM_MODELS` | you (optional) | no | Comma-separated models that write review comments. Empty means rule-based text only |
| `VREVIEW_MAX_UPLOAD_BYTES` | you (optional) | no | Default `2147483648` (2 GiB). Larger declared sizes get `413` |

`OMNIROUTE_API_KEY` and `OMNIROUTE_BASE_URL` are written `${VAR:?}` in the compose file, so Coolify refuses to deploy until both have a value.

Fixed in the compose file, not variables: `VREVIEW_STORE=r2`, bucket `vreview`, `VREVIEW_WORKER_SLOTS=2`, the Hatchet host (`hatchet:7077`, TLS off inside the private network), secure Hatchet cookies. Sign-up in the web app stays closed because `ALLOW_SIGNUP` is not set.

## First deploy

1. Create the resource (settings above), add the three domains, fill `OMNIROUTE_API_KEY` and `OMNIROUTE_BASE_URL`, then **Deploy**. The `web` image build is the slow step.
2. Expect `postgres`, `minio`, `hatchet`, `api` and `web` healthy. The `worker` restarts in a loop at this point because `HATCHET_CLIENT_TOKEN` is empty ("API token is required"). Do not submit reviews yet: with no token the API stores the run as `queued` and never hands it to Hatchet.
3. Create the Hatchet token on the server. Find the container (exactly one line must come back), then run the command:

   ```bash
   HATCHET=$(docker ps --filter "name=hatchet-<RESOURCE_UUID>" --format '{{.Names}}')
   echo "$HATCHET"
   docker exec "$HATCHET" /hatchet-admin token create --config /config --tenant-id 707d0855-80ab-4e1f-a156-f1c4546cbf52
   ```

   The last line of output is the token (a JWT starting `ey`). `<RESOURCE_UUID>` is in the Coolify URL of the resource. Coolify names containers `<service>-<resource uuid>-<timestamp>`.
4. Set `HATCHET_CLIENT_TOKEN` to that value in Coolify, then **redeploy**. The `api` and `worker` read it at start. After this the worker log shows `'vreview-worker' started, waiting for tasks...` and the worker turns healthy.
5. Create the first user (next section), open the web domain and sign in.

The token is tied to the signing keys in the `hatchet-config` volume. If that volume is lost, create a new token.

## Create users

Sign-up is closed. Create users inside the running web container (npm, not pnpm):

```bash
WEB=$(docker ps --filter "name=web-<RESOURCE_UUID>" --format '{{.Names}}')
docker exec "$WEB" npm run user:create -- --email you@example.com --password 'a-long-password' --name 'Your Name'
docker exec "$WEB" npm run user:set-password -- --email you@example.com --password 'a-new-long-password'
```

The password is an argument, so it is visible in the host's process list (`ps`, `docker top`) while the command runs and may end up in shell history. Use a one-off password and change it afterwards.

## Operating notes

- **Webhooks.** `webhook_url` must be `https` and its host must resolve to public addresses only. Private, loopback, link-local, multicast and reserved ranges (including IPv6 and IPv4-mapped IPv6) are rejected with `400` at `POST /v1/reviews` and the MCP `review_video` tool, and again right before each delivery. Delivery connects to the checked address and does not follow redirects. `VREVIEW_WEBHOOK_ALLOW_HTTP=1` allows plain http for development; it is not set here.
- **Model downloads.** Whisper, demucs and the MediaPipe face model download on the first review that needs them, so the worker needs outbound internet. They are cached in the `worker-data` volume (`HF_HOME`, `TORCH_HOME`, `~/.cache/dubeval`) and survive redeploys. Scratch files live in the same volume under `/data/scratch`; the entrypoint removes leftovers older than 24 hours on start.
- **Migrations.** Only `api` runs `alembic upgrade head` (`VREVIEW_RUN_MIGRATIONS=1`), retrying for about 30 s while Postgres comes up. The web image applies its own better-auth migrations on every start.
- **Hatchet dashboard.** It is not exposed. To use it, add a domain for `hatchet` on port `8888` and set `SERVER_URL` in the compose file to that URL; tokens embed `SERVER_URL`, so create a new token afterwards.
- **Changing a domain.** `NEXT_PUBLIC_VREVIEW_API_URL` is inlined into the web bundle, so a new `api` domain needs a rebuild of `web` (a normal redeploy does it). A new `web` domain also changes the JWT issuer, so existing sessions stop working.
- **Resource use.** The worker is capped at 6 GB RAM (no extra swap) and 4 CPUs. Idle usage observed in the smoke test: api about 210 MB, worker about 330 MB, Postgres about 280 MB, MinIO about 130 MB, Hatchet about 130 MB.

## Backups

Coolify's scheduled backups cover only its own database resources, so back these up from the host (cron or a script). Volume names are `<RESOURCE_UUID>_<volume>`.

| Volume | Holds | Back up |
|---|---|---|
| `postgres-data` | the three databases | yes, with `pg_dump` (not a copy of the live directory) |
| `minio-data` | uploaded and generated media | yes |
| `hatchet-config` | Hatchet signing keys and cookie secrets | yes, small and critical |
| `worker-data` | model caches and scratch | no, it refills |

```bash
PG=$(docker ps --filter "name=postgres-<RESOURCE_UUID>" --format '{{.Names}}')
for db in vreview hatchet vreview_auth; do
  docker exec "$PG" sh -c "pg_dump -U \"\$POSTGRES_USER\" -Fc $db" > "backup-$db-$(date +%F).dump"
done

# MinIO and Hatchet config: file-level copy of the volume (stop `minio` first for a consistent copy, or accept a live copy).
for vol in minio-data hatchet-config; do
  docker run --rm -v "<RESOURCE_UUID>_$vol:/src:ro" -v "$PWD:/out" alpine tar czf "/out/backup-$vol-$(date +%F).tgz" -C /src .
done
```

Restore a database into an empty stack with `docker exec -i "$PG" sh -c 'pg_restore -U "$POSTGRES_USER" -d <db> --clean --if-exists' < backup-<db>.dump`; restore volumes by extracting the tarball into the volume while the service is stopped. Test a restore once before relying on the backups.

## Rollback

1. **Code.** In Coolify set the resource's branch or commit to the last good commit and redeploy. Images are rebuilt from that commit.
2. **Schema.** Migrations do not roll back on their own. Check whether the migrations added since the good commit are additive (new tables or nullable columns): older code then runs on the newer schema as is. If not, run the downgrade before redeploying the old code, from the still-running new `api` container: `docker exec "$API" alembic downgrade <revision>`. If a migration lost data or cannot be undone, restore the `vreview` dump taken before the deploy. Take a `pg_dump` before every deploy that includes a migration.
3. **Web logins.** better-auth migrations also run on every start and are not reverted by a code rollback. Restore the `vreview_auth` dump if you need to undo one.
4. **Config.** Coolify keeps the current variable values only. Note the old value before changing a variable. Rotating `SERVICE_PASSWORD_POSTGRES` or `SERVICE_USER_POSTGRES` after the first start does not change the role inside the existing database; keep those values stable.
5. **Hatchet.** The image tag is pinned in the compose file. Do not roll Hatchet back across versions without restoring its database dump, because it migrates its own schema on start.
