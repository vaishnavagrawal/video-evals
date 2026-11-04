# vreview: code review for dubbed and AI-generated video

AI dubbing and video generation produce more footage than people can check. vreview reviews a video the way a
senior engineer reviews a pull request. You upload the media and a brief, and it returns timestamped threads
(blocker, should-fix, needs-human, nit) with evidence and a fix instruction a generator can act on. Results
come back over REST, MCP or a webhook, and a review UI plays the original and the dub side by side.

Built for the Meta Global AI Developer Hackathon (Nov 2026). Theme: **Build for Your World**. Meta's **Muse
Spark** watches and listens to the video, and deterministic signal processing keeps it honest.

<!-- DEMO: link to the demo video / GIF here -->

## How Meta's models are used

| Role | Model (default config) | What it does |
|---|---|---|
| Audio-video judge (`av_judge`) | `meta:muse-spark-1.3` | Gets short clips of the original and the dub (video + audio, `video_url` parts) around every located issue. Confirms or disputes each issue and reports what the other checks can't see: lip sync, a voice that doesn't fit the speaker, lost emotion, TTS artifacts, visual glitches, brief mismatch |
| Dub text judge | `meta:muse-spark-1.3` + `gateway:gemini-flash`, ensemble | Compares the original and dubbed transcripts line by line for meaning, missing or added content, tone and names. Both models judge every chunk, findings are merged, and agreement raises confidence |
| Review writer | `meta:muse-spark-1.3` | Writes each thread's title, comment and the review summary. It cannot change a priority, a timestamp or the set of issues |

One rule shapes the design: **timestamps only come from deterministic layers** (VAD, alignment, ASR word
times, shot and face analysis). The models judge evidence that is already located. A new finding from the
audio-video judge inherits its window's bounds, and a disputed issue keeps its time but drops to
`needs-human`. That's the defence against hallucinated issues.

Every model is a config value, so nothing is tied to Meta or any other vendor (see [Models and providers](#models-and-providers)).

## Pipeline

```
upload (presigned multipart) ─▶ POST /v1/reviews ─▶ Hatchet DAG
  ingest ─▶ plan tracks ─▶ features (child run per audio track: ASR, separation)
         ─▶ pairs (child run per dub: timing, sync, text judge, mouth activity)
         ─▶ assemble ─▶ audio-video judge (Muse Spark) ─▶ triage + LLM comments ─▶ review.json, DB, webhook
```

Each stage writes pipeline events: status, duration, sub-runs, models used, issue counts, and whether the
result came from cache. `GET /v1/reviews/{id}/pipeline` returns them, and the review page draws them live as
a timeline. Stage results are cached by content hash and config, so changing one model only reruns the
stages that use it.

## What's in the repo

| Path | What |
|---|---|
| `src/dubeval/` | Dubbing QA engine (also a standalone CLI): tracks, ASR, alignment, timing checks, text judge, mouth activity |
| `src/dubeval/providers.py` | Model registry: `provider:model` references to OpenAI-compatible clients |
| `src/vreview/evaluators/` | Evaluator plugins: `dubbing`, `av_judge` |
| `src/vreview/workflows/review.py` | Hatchet DAG, child runs, retries, per-tenant concurrency |
| `src/vreview/triage.py`, `triage_llm.py` | Code-review triage (priorities, threads, verdict), LLM-written comments |
| `src/vreview/api/` | FastAPI REST under `/v1`, MCP at `/mcp` (`review_video`, `get_review`, `get_issues`, `get_pipeline`) |
| `web/` | Next.js + better-auth: dashboard, upload, review player with issue timeline, pipeline view, feedback |
| `deploy/` | Local compose stack, Coolify production compose and runbook |
| `docs/` | Architecture, triage/UI contract, status and pending work |

## Models and providers

A model reference is `provider:model`. A bare `model` goes to `LLM_DEFAULT_PROVIDER` (default `gateway`).
A provider is any OpenAI-compatible endpoint, configured only through the environment:

```bash
META_API_KEY=...                                  # preset: https://api.meta.ai/v1, takes video_url
LLM_PROVIDER_FIREWORKS_BASE_URL=...               # override or add any provider by name
LLM_PROVIDER_MYVLLM_BASE_URL=http://vllm:8000/v1  # e.g. self-hosted Llama / Muse Spark Open
LLM_PROVIDER_MYVLLM_API_KEY_ENV=VLLM_TOKEN
LLM_PROVIDER_MYVLLM_VIDEO=frames                  # video_url | frames | none

VREVIEW_JUDGE_MODELS=meta:muse-spark-1.3,gateway:gemini-flash
VREVIEW_JUDGE_MODE=ensemble                       # or fallback
VREVIEW_TRIAGE_LLM_MODELS=meta:muse-spark-1.3,gateway:gemini-flash
VREVIEW_AV_JUDGE_MODELS=meta:muse-spark-1.3
```

Each role takes a fallback list that can mix vendors, and a brief can override any of them for a single
review (`options.dubbing.llm_models`, `options.av_judge.models`). Providers that only take images get frames
instead of clips, and the prompt tells the model it has no audio.

## Run locally

```bash
uv sync --extra platform
docker compose -p vreview -f deploy/docker-compose.yml up -d postgres minio minio-init hatchet
cp .env.example .env            # add META_API_KEY and the Hatchet token (deploy/README.md)
uv run alembic upgrade head
uv run vreview-api              # :8000
uv run vreview-worker
cd web && pnpm install && pnpm dev   # :53000, see web/README.md
```

Tests: `uv run pytest` (backend), `cd web && pnpm build`.

## Results

<!-- E2E: fill in from the verified run -->

## Known limits

- Accuracy is unmeasured: there's no labelled golden set yet. Reviewer feedback (`accept` / `false_positive`)
  is stored per issue as the seed for one.
- Model findings vary between runs. The ensemble judge and caching reduce this, but don't remove it.
- Only Hindi→Spanish has been tested on real footage. Other languages are covered by unit tests only.
- Muse Voice Transcribe returns turn-level timestamps, and the timing checks need word-level times, so ASR
  stays on Whisper for now.
- Reviews run on CPU. Long videos are slow until the feature stage moves to GPU workers.

More: `docs/pending.md`, `docs/gaps-and-roadmap.md`.
