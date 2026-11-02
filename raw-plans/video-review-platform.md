# Video Review Platform: MVP Plan

Implementation (2026-10-31): plan items 1-3 are built in `src/vreview` (storage/ingest, dubbing evaluator stages, Hatchet `review` workflow + worker, FastAPI + FastMCP + Postgres + better-auth JWT, webhooks), with a local stack in `deploy/` (see `deploy/README.md`). Verified end to end on a 60 s clip. Not built yet: web app (item 4), Modal GPU offload, evaluator #2, review agent, golden sets.
Status: raw plan, updated 2026-10-31. Source PoC: `src/dubeval` (dubbing QA).

## Product

A code-review agent for AI-generated video. A generation pipeline or agent submits a video plus its brief (prompt, script, storyboard, references). We return a structured review: timestamped, shot-scoped issues with evidence and fix hints that the generator can act on automatically. Dubbing QA is the first evaluator. Visual consistency, directorial and audio checks follow.

Three ways this differs from the PoC:
1. Evaluators are plugins; dubbing is just plugin #1.
2. The main consumer is a machine (a generation pipeline or agent), so the output is a fix-instruction contract.
3. Which checks to run depends on the brief, and ambiguous issues need investigation. That is where an agent layer earns its place.

## Architecture

```
Generation pipelines / agents ──(REST, webhooks, MCP)───────┐
Web app (Next.js/TanStack, better-auth) ── review UI ───────┤
                                                            ▼
                         FastAPI + FastMCP ── Postgres (+pgvector) ── Cloudflare R2
                                                            │ enqueue
                                               Hatchet engine (self-hosted, Coolify)
                                                            │ gRPC
                         Python Hatchet workers (CPU on Coolify; GPU stages → Modal)
             ┌──────────────────────────────────────────────┼──────────────────────┐
     1. Perception layer (deterministic, cached)    2. Evaluators (plugins)   3. Review agent
     shots, ASR, stems, faces, embeddings,          dubbing, consistency,     plans, investigates,
     OCR, motion, keyframes                         audio, adherence, ...     writes review
```

### 1. Perception layer
Compute once and share across evaluators. Cache key: `content_hash + extractor_version + config_hash`, stored in R2. This generalizes the PoC's `work/` cache.
- Shots: TransNetV2 / PySceneDetect
- ASR + LID: faster-whisper. Stems: Demucs / BS-RoFormer
- Faces / identity: InsightFace. Embeddings: DINOv2 / SigLIP, stored in pgvector
- Keyframes, OCR (garbled text), optical flow

This layer is Python and GPU-heavy, and it's where most of the cost goes.

### 2. Evaluator interface
```python
class Evaluator(Protocol):
    id: str; version: str
    requires: set[str]           # perception features, e.g. {"shots", "faces", "asr"}
    def applies_to(self, brief) -> bool: ...
    def run(self, ctx) -> list[Issue]: ...
```
Invariant carried over from the PoC: timestamps come only from deterministic layers. The LLM/VLM judges evidence that's already located and never invents times. This is the main defense against hallucinated issues.

Evaluators:
- Dubbing (port of the current pipeline)
- Temporal/identity consistency: face/embedding drift, flicker, morphing
- Physics/artifacts: hands, text, object permanence (VLM + VBench-style signals)
- Prompt/brief adherence (VLM vs script/storyboard)
- Directorial: shot rhythm, framing, continuity, pacing
- Audio: lip-sync (SyncNet-style), LUFS, clipping, background dropouts

### 3. Review agent
Inputs: the brief plus perception summaries. It picks evaluators, investigates ambiguous issues through tools (`get_frames(t0,t1)`, `crop`, `compare_shots`, `rerun_evaluator`), dedupes and ranks, then writes the review. It's bounded: a step budget, and every issue must cite tool evidence.

Models: Gemini for native video understanding, self-hosted Qwen-VL via vLLM for cost and privacy, LiteLLM for routing. Benchmark them on the golden set before committing.

## Output contract
```
{category, severity, t0, t1, shot_id, evidence[frames], fix_hint,
 suggested_prompt_patch, regenerate_scope: "shot"|"segment"|"full"}
```
- Integrations: REST, a completion webhook, an MCP server (`review_video`, `get_issues`). ComfyUI and n8n later.
- Re-review diffing: on v2 of a video, report which issues are fixed, which persist and which are new.

## Data model (Postgres)
`projects`, `media_assets` (content hash, R2 key), `briefs`, `runs` (Hatchet workflow run id), `reviews` (jsonb), `issues` (rows), `issue_feedback` (accept / false positive / comment). Store the feedback: it becomes the eval dataset.

## Review UI
A player with an issue timeline track. Clicking an issue jumps to the frames, a side-by-side view covers shot comparisons, and each issue gets feedback actions.

## Evaluating the evaluators
- One golden set per evaluator, including defects planted on purpose (freeze frames, face swaps, audio shifts, garbled text).
- Time-overlap precision and recall per category (IoU ≥ 0.3), plus onset error. Gate CI on it per evaluator version.
- Tracing: Langfuse or Logfire via OpenTelemetry. DeepEval/promptfoo only for text-level judge prompts. Neither fits timestamped detection metrics, so that harness is custom.

## Decision (final)

### Orchestration: Hatchet (self-hosted on Coolify)
- Each review is a Hatchet DAG workflow; each stage is a task with its own retries, timeout and concurrency key. A failed task retries alone, and finished parents are not rerun.
- Shape: `ingest → probe → [stems, shots, asr, faces, embeddings] (parallel) → evaluators (one task each, fan-out) → review_agent → persist + webhook`.
- Concurrency keys: per tenant (fair share, quotas) and per GPU stage (cap Modal spend).
- Hatchet's dashboard gives run inspection, replay and cancel for free; the app stores only the Hatchet run id plus its own status mirror.
- Workers are plain Python processes (Hatchet SDK, Pydantic inputs/outputs). CPU tasks run in a Coolify worker container. GPU tasks are Hatchet tasks that call Modal functions and return R2 keys; Modal holds no state.
- Idempotency: every task first checks the R2 content-hash cache, so retries and re-reviews skip finished work.

### Agent: pydantic-ai inside Hatchet tasks
- pydantic-ai has no native Hatchet integration (it ships DBOS, Temporal, Prefect and Restate adapters). So the review agent runs as one Hatchet task, and its durability is that task's retry.
- That's acceptable because the agent's expensive inputs (perception, evaluator outputs) are already persisted upstream tasks. Make model and tool calls cheap to replay: cache LLM/VLM responses by request hash (as `judge.py` does today) and keep tools read-only over R2/Postgres.
- If agent runs get long (many tool steps), split investigation into child workflows spawned from the agent task.

### Serving
- FastAPI: REST + webhooks, enqueue via the Hatchet client, verify better-auth JWTs against its JWKS endpoint.
- FastMCP server mounted on the same app: `review_video`, `get_review`, `get_issues`.
- Workers and API share the Python package (evaluators, schema), deployed as separate containers.

### Storage: Cloudflare R2
- S3-compatible API: boto3 with `endpoint_url=https://<account_id>.r2.cloudflarestorage.com`, `region_name="auto"`. Nothing else in the stack needs to change.
- Browser uploads via presigned multipart URLs issued by FastAPI (or Next.js). Set a CORS policy on the bucket for the web origin.
- Zero egress fees matter here: Modal workers and the review player read large media repeatedly.
- Layout: `media/<content_hash>.<ext>`, `cache/<content_hash>/<extractor>@<version>/<config_hash>/...`, `reviews/<run_id>/frames/...`.
- Serve frames and media to the UI through short-lived presigned GET URLs; the bucket stays private.

### Deployment on Coolify
Services:
- `postgres` (pgvector image): app DB. Hatchet gets a separate database (or a separate instance) on it, so workflow churn doesn't touch app tables or migrations.
- `hatchet`: `hatchet-lite` image with `SERVER_MSGQUEUE_KIND=postgres` for the MVP (no RabbitMQ). The Hatchet docs label lite as "development and low-volume"; move to the split `hatchet-engine` + `hatchet-dashboard` + RabbitMQ compose when volume grows.
- `api`: FastAPI + FastMCP.
- `worker`: Hatchet worker (CPU tasks; GPU tasks call Modal).
- `web`: Next.js + better-auth.

Config notes:
- Pin Hatchet image tags (the docs default to `latest`).
- Expose the Hatchet gRPC port (7077) only on Coolify's internal network; set `SERVER_GRPC_BROADCAST_ADDRESS` to the internal hostname. Re-issue worker tokens if that address changes.
- Put the Hatchet dashboard behind Coolify's proxy with TLS. Change the default admin credentials (`admin@example.com` / `Admin123!!`) on first boot.
- Back up Postgres (both DBs). R2 is the source of truth for media and caches.

### Why not DBOS / Agno
- DBOS: chosen first for zero-infra, but Hatchet adds a run dashboard, fan-out DAGs, per-tenant concurrency and a separate worker fleet, which a multi-tenant GPU pipeline needs soon after launch. With Coolify, running Hatchet costs little extra.
- Agno: whole-run retries and in-process execution don't suit long GPU pipelines; what it saves (API/MCP/auth glue) is the cheap part to build.

Risks: Hatchet is one more stateful service to run and upgrade (pin versions, test upgrades on staging). pydantic-ai agent tasks retry from the start, so keep them short and their calls cached.

## Stack
| Layer | Choice |
|---|---|
| Web | Next.js / TanStack, better-auth (JWT/JWKS plugin), Drizzle |
| DB | Postgres + pgvector (app DB; Hatchet in a separate database) |
| Storage | Cloudflare R2 (S3 API, presigned multipart uploads, private bucket) |
| Orchestration | Hatchet self-hosted (hatchet-lite, Postgres MQ for MVP) |
| Workers | Python (uv), Hatchet SDK; CPU on Coolify, GPU stages on Modal |
| Agent | pydantic-ai inside Hatchet tasks, LiteLLM routing, cached model calls |
| API | FastAPI + FastMCP |
| Perception | faster-whisper, Demucs, TransNetV2, InsightFace, DINOv2/SigLIP, OCR |
| Models | Gemini (video), Qwen-VL via vLLM, OpenAI/Claude for text judging |
| Evals | Custom time-aware harness (pytest), Langfuse/Logfire datasets from user feedback; promptfoo for judge prompts |
| Deploy | Coolify (postgres, hatchet, api, worker, web) |
| Later | Full Hatchet compose with RabbitMQ at volume; self-hosted GPU workers if Modal cost dominates; Mastra for a TS-side review chat |

## MVP sequencing
1. Extract the perception layer and `Evaluator` interface from `src/dubeval`; port dubbing as plugin #1, with content-hash R2 caching.
2. Turn `pipeline.run_pair` into a Hatchet DAG (one task per stage); GPU stages call Modal. Stand up hatchet-lite + Postgres on Coolify.
3. Build FastAPI + FastMCP (better-auth JWT via JWKS, R2 presigned uploads, completion webhook).
4. Build the web app: upload, review UI, issue feedback.
5. Add evaluator #2, identity/temporal consistency (shots + embeddings + VLM).
6. Add the review agent (pydantic-ai: evaluator routing + investigation tools) and re-review diffing.
7. Build the golden sets and CI eval gate per evaluator.
