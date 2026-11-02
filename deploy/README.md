# Deploying vreview

This directory contains the orchestration and infrastructure definitions for vreview.

## Services & Port Allocations

| Service | Internal Port | Host Port (Local) | Purpose |
|---|---|---|---|
| `postgres` | 5432 | 55433 | App database (`vreview`) and workflow engine database (`hatchet`) |
| `minio` | 9000 (S3), 9001 (Console) | 59000, 59001 | S3-compatible blob storage (R2 emulation) |
| `hatchet` | 8888 (Web), 7077 (gRPC) | 58888, 57077 | Hatchet-lite workflow orchestrator (Postgres MQ) |
| `api` | 8000 | 58000 | FastAPI + FastMCP HTTP application |
| `worker` | N/A | N/A | Hatchet Python worker for workflow task execution |

---

## Local Development Setup

### 1. Start Infrastructure Services

Bring up Postgres, MinIO, bucket initialization, and Hatchet-lite:

```bash
docker compose -p vreview -f deploy/docker-compose.yml up -d postgres minio minio-init hatchet
```

Verify service health:
```bash
docker compose -p vreview -f deploy/docker-compose.yml ps
```

### 2. Generate Hatchet Worker API Token

Generate an API token from the running Hatchet container:

```bash
docker compose -p vreview -f deploy/docker-compose.yml exec hatchet /hatchet-admin token create --config /config --tenant-id 707d0855-80ab-4e1f-a156-f1c4546cbf52
```

Copy the generated token into `.env` as `HATCHET_CLIENT_TOKEN`.

### 3. Configure Local Environment (`.env`)

Create `.env` from `.env.example`:

```bash
cp .env.example .env
```

Ensure the following connection parameters are configured for host execution:
- `VREVIEW_DATABASE_URL=postgresql+psycopg://vreview:vreview@localhost:55433/vreview`
- `VREVIEW_R2_ENDPOINT_URL=http://localhost:59000`
- `VREVIEW_R2_PUBLIC_ENDPOINT_URL=http://localhost:59000`
- `VREVIEW_R2_ACCESS_KEY_ID=minioadmin`
- `VREVIEW_R2_SECRET_ACCESS_KEY=minioadmin`
- `VREVIEW_R2_BUCKET=vreview`
- `HATCHET_CLIENT_HOST_PORT=localhost:57077`
- `HATCHET_CLIENT_TLS_STRATEGY=none`
- `HATCHET_CLIENT_TOKEN=<token from step 2>`

### 4. Run App Migrations & Services on Host

```bash
# Run database migrations
uv run alembic upgrade head

# Start API
uv run vreview-api

# Start worker
uv run vreview-worker
```

---

## Deployment on Coolify

1. **Stack Setup**: In Coolify, create a new Docker Compose project referencing `deploy/docker-compose.yml`.
2. **Cloudflare R2**: In production, replace MinIO with Cloudflare R2 credentials:
   - `VREVIEW_STORE=r2`
   - `VREVIEW_R2_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com`
   - `VREVIEW_R2_PUBLIC_ENDPOINT_URL=` (leave empty: presigned URLs must be signed against the S3 API endpoint; a public r2.dev/custom domain does not accept SigV4 presigned requests)
   - Add a bucket CORS rule allowing `PUT` and `GET` from the web origin and exposing the `ETag` header (the browser needs it to complete multipart uploads).
   - `VREVIEW_R2_ACCESS_KEY_ID=<R2 Access Key>`
   - `VREVIEW_R2_SECRET_ACCESS_KEY=<R2 Secret Key>`
   - `VREVIEW_R2_BUCKET=<bucket_name>`
3. **Hatchet Production Settings**:
   - `SERVER_AUTH_COOKIE_INSECURE=f`
   - Configure a custom domain and SSL certificate for the Hatchet dashboard (port 8888).
   - Change default admin credentials (`admin@example.com` / `Admin123!!`) after initial boot.
   - Keep gRPC port 7077 internal to the Docker network (`SERVER_GRPC_BROADCAST_ADDRESS=hatchet:7077`).
4. **App & Worker**:
   - Build both services from the single `Dockerfile`.
   - Scale `worker` instances based on processing queue load.
