# vreview MVP: status summary

Updated 2026-11-02. Full plan and design rationale: `raw-plans/video-review-platform.md`. PoC internals: `docs/architecture.md`, `docs/gaps-and-roadmap.md`.

## What it is

A review service for dubbed and AI-generated video, built like a code-review agent. A client uploads media plus a brief and gets back timestamped issues with fix hints, over REST, MCP or a webhook. Dubbing QA is the first evaluator. It wraps the `src/dubeval` PoC, which still works on its own (`dubeval` CLI).

## Stack

| Layer | Choice |
|---|---|
| Orchestration | Hatchet, self-hosted (`hatchet-lite` v0.114.2, Postgres as message queue) |
| Storage | Cloudflare R2 (S3 API); MinIO locally |
| DB | Postgres 16 + pgvector, SQLAlchemy 2, Alembic |
| API | FastAPI + FastMCP 4.1.4 |
| Auth | better-auth JWTs, verified against its JWKS (EdDSA/RS256/ES256) |
| Compute | Python 3.12 / uv, CPU torch; faster-whisper, Demucs, MediaPipe 0.10.35 |
| Deploy target | Coolify |

## What's built (plan items 1-3)

| Area | Files | Notes |
|---|---|---|
| Contracts | `src/vreview/schema.py`, `evaluator.py`, `jobs.py`, `config.py` | `vreview.v1` review schema; `Evaluator` protocol; settings from `VREVIEW_*` env |
| Storage | `src/vreview/storage/{base,r2,local}.py`, `ingest.py` | Content-addressed keys (`media/<sha256>`, `cache/<sha256>/<stage>@<ver>/<cfg_hash>`, `reviews/<run_id>`); presigned multipart uploads; https-only URL ingest, 4 GiB cap |
| Dubbing evaluator | `src/vreview/evaluators/dubbing/{models,stages}.py` | `plan_tracks → track_features → run_pair → assemble`; each stage is cached on a config subset, so changing the LLM model doesn't redo ASR |
| Workflow | `src/vreview/workflows/review.py`, `worker.py`, `hatchet.py` | DAG `ingest → dubbing_plan → dubbing_features (child run per track) → dubbing_pairs (child run per dub) → assemble → finalize`; retries/timeouts per task; per-tenant concurrency; failure handler marks the run failed; run row records the current stage |
| API | `src/vreview/api/*`, `db/*`, `webhooks.py`, `alembic/` | REST under `/v1` (below), MCP at `/mcp`, `/healthz`; every query tenant-scoped (tenant = JWT `sub`); HMAC-signed webhooks |
| Infra | `deploy/docker-compose.yml`, `deploy/README.md`, `Dockerfile`, `.env.example` | One image for api + worker |

REST endpoints (`/v1`):
- `POST /uploads`, `POST /uploads/{asset_id}/complete`
- `POST /reviews`, `GET /reviews`, `GET /reviews/{run_id}`
- `GET /reviews/{run_id}/issues?severity=&category=&evaluator=`
- `GET /reviews/{run_id}/artifacts/{name}` (presigned URL)
- `POST /reviews/{run_id}/issues/{issue_id}/feedback` (`accept` | `false_positive` | `comment`)

MCP tools: `review_video`, `get_review`, `get_issues`.

## Verified

- `uv run pytest`: 59 passed (storage, dubbing stages, workflow, API auth/endpoints/MCP/webhooks, languages).
- End to end on a 60 s clip pair: JWT auth → multipart upload to MinIO → Hatchet worker → succeeded in 150 s, dubbing score 7.1, 21 issues (3 critical), feedback saved, presigned `review.json` download worked. No token → 401; another tenant → 404.
- Not verified: real Cloudflare R2, a real better-auth instance, Coolify deployment, runs on full-length videos through the platform, behavior under concurrent load.

## Run locally

```bash
uv sync --extra platform
docker compose -p vreview -f deploy/docker-compose.yml up -d postgres minio minio-init hatchet
uv run alembic upgrade head
uv run vreview-api      # :8000 by default; compose maps the container to host 58000
uv run vreview-worker
```
Host ports: Postgres 55433 (55432 is taken by a Coolify Postgres on this machine), MinIO 59000/59001, Hatchet UI 58888, gRPC 57077. `.env` holds the local Hatchet token and is gitignored. Hatchet token creation: see `deploy/README.md`.

## Changes from the plan

- `mediapipe` 0.10.21 → 0.10.35, forced by hatchet-sdk's `protobuf>=6`. `src/dubeval/visual.py` moved from `mp.solutions.face_mesh` to the Tasks `FaceLandmarker`. The model downloads on first use to `~/.cache/dubeval/face_landmarker.task` and is checked against a pinned sha256.
- pydantic-ai has no Hatchet adapter, so when the review agent comes it runs inside a single Hatchet task. Keep its model/tool calls cached so retries are cheap.
- Small dubeval refactors (`pipeline.py`, `speech.py`) add seams for the staged evaluator. CLI behavior is unchanged.

## Pending

Product (plan items 4-7):
1. Next.js web app + better-auth: upload UI (presigned multipart), review player with issue timeline, feedback actions.
2. GPU offload: move `track_features` (ASR, Demucs) to Modal. It only depends on its arguments, so it can move as is.
3. Evaluator #2: identity/temporal consistency (shot detection, face/embedding drift, VLM judge).
4. Review agent (pydantic-ai): picks evaluators by brief, investigation tools, re-review diffing between versions.
5. Golden sets + time-overlap precision/recall harness; CI gate per evaluator version.

Before a real deployment:
- Replace dev defaults: MinIO `minioadmin`, Hatchet admin `admin@example.com` / `Admin123!!`, `SERVER_AUTH_COOKIE_INSECURE=t`, `MINIO_API_CORS_ALLOW_ORIGIN=*`, `VREVIEW_WEBHOOK_SECRET`.
- R2: bucket CORS for `PUT`/`GET` from the web origin, exposing `ETag`; leave `VREVIEW_R2_PUBLIC_ENDPOINT_URL` empty (presign against the S3 API endpoint).
- better-auth: enable the JWT plugin; set `VREVIEW_AUTH_JWKS_URL` (default path `/api/auth/jwks`), and set `VREVIEW_AUTH_ISSUER` / `VREVIEW_AUTH_AUDIENCE` to match its claims.
- Coolify: keep Hatchet gRPC (7077) internal-only; put the dashboard behind TLS; give Hatchet its own database (already separate: `hatchet`); back up Postgres.
- Git: the platform work is committed on `master` (`vreview: review platform MVP over the dubeval PoC`, plus this docs commit). No remote is configured yet, so nothing is pushed or backed up off this machine.

## Pointers and known gaps

- Review triage + web app (2026-11-03, spec `docs/triage-and-ui-contract.md`):
  - Code-review triage: priority `blocker | should-fix | needs-human | nit` (rule-based), areas, tags, threads, verdict. LLM (`VREVIEW_TRIAGE_LLM_MODELS`) writes thread comments and the summary only, cached, best-effort with a 600 s cap.
  - `web/`: Next.js + better-auth (email/password, JWKS on :53000), dashboard, upload (candidate + original + reference images, multipart, retry, cancel/abort), review page (synced players, timeline, threads, feedback that survives reload). Run: `web/README.md`.
  - Verified end to end on a 60 s pair through the browser: upload -> worker -> `request_changes`, score 2.1, 9 threads, all comments LLM-written.
  - Gaps: runs whose worker dies stay `running` forever (e.g. `run_258c273815b1` from 2026-10-31), nothing reconciles them. A low score can sit next to "Approved" (10 s clip: 3.5 / approve) because the score penalizes every issue while the verdict only looks at blockers; the UI should explain or the score needs normalizing. The LLM judge sometimes labels a missing dub as `UNTRANSLATED`, which adds a false `language` area. The LLM summary is accurate but formulaic. Dev DB run `run_01843ba87dc7` has probe feedback rows from UI testing.
- Security:
  - URL ingest (`ingest.py`) allows any https host, so it can be used to reach internal services (SSRF). Before opening to users, block private/link-local IPs after DNS resolution, or restrict ingest to uploads.
  - Webhook URLs aren't checked against private addresses either (same SSRF risk).
  - `GET /reviews/{run_id}/artifacts/{name}` passes `name` straight into the key. With R2, `..` doesn't escape the prefix (S3 keys aren't normalized), but `LocalStore` could. Validate `name` (no `..`, no leading `/`) before any non-test use.
  - No rate limits or quotas yet (max video length, reviews per tenant per day). Add them at the API, plus a cap on Modal spend through Hatchet concurrency.
- Workflow:
  - `hatchet-lite` is documented as dev/low-volume. Move to the split engine + dashboard + RabbitMQ setup when volume grows.
  - A worker stopped with SIGTERM can take more than 10 s to exit (seen in the smoke run). Give it a long enough stop grace period in Coolify so in-flight tasks aren't cut off mid-write.
  - Child-run results are read from the Hatchet output dict by task name (`dubbing_features_task`); keep task names stable.
- Pipeline:
  - First run downloads Whisper and the FaceLandmarker models. Bake them into the image, or into a cache volume, so cold starts aren't slow.
  - Cutting test clips with `ffmpeg -ss ... -c copy` past the end of the source (the sample is 356 s) gives near-empty files and an opaque `v cannot be empty` failure. Ingest should reject media with no usable audio/duration, with a clear error.
  - LLM judge credentials come from `OMNIROUTE_*` / `DUBEVAL_LLM_*`. If the upstream model hits rate limits (429s were seen during development), judged issues drop out silently and only show up as warnings.
- Data: issue feedback (`issue_feedback`) is the seed for the golden sets. Keep it from day one, and export it by evaluator version.
