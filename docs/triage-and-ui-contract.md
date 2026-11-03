# Review triage and web UI: shared contract

Status: agreed 2026-11-03. Three work streams build against this file in parallel. If a stream needs to
change a shape here, it says so in its final report instead of silently diverging.

## 1. Triage model (code-review style)

Triage is a deterministic layer over evaluator issues. It never changes `severity`, `t0`/`t1` or the
score. The LLM only writes prose (thread titles, comments, the review summary).

### Priority (per issue, then per thread)

`blocker` | `should-fix` | `nit` | `needs-human`

Rule, applied in order (thresholds live in `vreview.triage.TriageConfig`):
1. `details.source == "language"` and severity `critical` -> `blocker` (wrong/untranslated language is never ambiguous).
2. LLM-sourced issue (`details.source == "semantic"`) with `confidence < 0.6` -> `needs-human`.
3. Any issue with `confidence < 0.4` -> `needs-human`.
4. Otherwise by severity: `critical` -> `blocker`, `major` -> `should-fix`, `minor` -> `nit`.

Do not re-apply the visual "is a face speaking" adjustment: `dubeval.visual.adjust` already folded it
into `severity`.

Thread priority = highest member priority, ordering `blocker > should-fix > needs-human > nit`.

### Areas (fixed category -> area map, no LLM)

| area | categories |
|---|---|
| `accuracy` | MEANING_MISMATCH, MISSING_CONTENT, ADDED_CONTENT, NAME_OR_TERM_ERROR |
| `sync` | LATE_START, EARLY_START, EARLY_CUTOFF, OVERRUN, SYNC_DRIFT, MID_LINE_GAP, MISSING_DUB, UNMATCHED_SPEECH, DURATION_MISMATCH |
| `audio-mix` | BACKGROUND_DROPOUT, ENERGY_MISMATCH |
| `delivery` | TONE_MISMATCH, FAST_SPEECH |
| `script` | SCRIPT_DEVIATION, SUBTITLE_AUDIO_DESYNC |
| `language` | WRONG_LANGUAGE, UNTRANSLATED |
| `video` | VIDEO_MISMATCH |
| `other` | anything unmapped |

### Tags (per issue, list of strings)

- `area:<area>` (exactly one)
- `on-camera` when `details.visual.speaking_likely is True`
- `llm` when `details.source == "semantic"` (these were only 18-25% repeatable across runs; the UI shows this)

### Threads

A thread is one review comment, like a comment on one line of code. Cluster issues of the same `track`
whose `[t0, t1]` intervals overlap or are within 0.3 s, capping a thread at 15 s span (start a new thread
past the cap). Thread ids: `T001`, `T002`, ... in time order within the review (not stable across runs,
same as issue ids).

### Verdict (per review)

- `needs_human` if no evaluator result has status `ok` (nothing was actually checked)
- else `request_changes` if any blocker thread
- else `needs_human` if any needs-human thread
- else `approve` (should-fix and nit threads may remain; the UI shows them as comments)

`counts` and `area_counts` count **threads**, not issues (a thread counts once per area it contains).
`counts` always has all four keys; `area_counts` is sorted largest first.

### Decisions made during implementation (2026-11-03)

- `Thread` and `Triage` are defined in `vreview.schema` (re-exported by `vreview.triage`) to avoid a circular import.
- Threads carry optional `comment_model`; triage carries optional `summary_model` (which LLM wrote the text).
- `runs.media` (migration `0002`) links a run to its uploaded assets, so queued/failed runs have `filenames`
  and the media endpoint works before ingest.
- `priority` / `area` / `thread_id` issue filters run in Python so older reviews get triage on the fly;
  invalid values return 422. Triage computed on read is not written back.
- Uploads also reject filenames containing `/`, `\`, NUL, or equal to `.`/`..` (400).
- `plan_tracks` drops `reference` media up front so attaching one doesn't change the cached plan key.

## 2. Data shapes

### Issue (additions to `vreview.schema.Issue`, all with defaults so old data still parses)

```json
{ "priority": "should-fix", "tags": ["area:sync", "on-camera"], "area": "sync", "thread_id": "T004" }
```

### Review (addition to `vreview.schema.Review`): `triage`

```json
{
  "triage": {
    "version": "1",
    "verdict": "request_changes",
    "counts": { "blocker": 12, "should-fix": 20, "needs-human": 6, "nit": 15 },
    "area_counts": { "sync": 31, "accuracy": 9, "audio-mix": 3, "delivery": 2, "script": 1 },
    "threads": [
      {
        "id": "T004",
        "track": "hi->es",
        "t0": 46.78, "t1": 48.35,
        "priority": "blocker",
        "areas": ["sync", "accuracy"],
        "issue_ids": ["dubbing:hi->es:I013", "dubbing:hi->es:I014"],
        "title": "Dub cuts off while the actor is still speaking",
        "comment": "Markdown. What is wrong, why it matters, how to fix.",
        "comment_source": "rule",
        "original_text": "...", "dubbed_text": "..."
      }
    ],
    "summary": "Markdown, 2-5 sentences.",
    "summary_source": "rule",
    "llm_warnings": []
  }
}
```

`comment_source` / `summary_source`: `"rule"` (template text built from member issues) or `"llm"`.
Rule text must always be present, so a failed or disabled LLM still yields a complete review.

### Python API

- `vreview.triage.triage(review: Review, cfg: TriageConfig | None = None) -> Triage` - pure, deterministic, no I/O.
  Also sets `priority`/`tags`/`area`/`thread_id` on each issue of `review` (returns the triage; mutates issues).
- `vreview.triage_llm.write_comments(threads: list[Thread], issues_by_id: dict[str, Issue], *, src_lang: str | None,
  tgt_lang: str | None, models: list[str], cache: BlobStore, cache_prefix: str) -> LLMComments`
  where `LLMComments = {"threads": {thread_id: {"title": str, "comment": str}}, "summary": str | None, "warnings": [str]}`.
  Cached per input hash in blob storage; never raises (failures go to `warnings`).
- Wiring: `finalize_task` runs `triage`, then `write_comments` if `VREVIEW_TRIAGE_LLM_MODELS` is non-empty, and
  stores the result in `review.triage`. `GET /v1/reviews/{run_id}` computes rule-only triage on the fly for
  reviews stored without one (older runs).

## 3. API additions (all under `/v1`, tenant-scoped like the rest)

| method | path | notes |
|---|---|---|
| `GET` | `/reviews/{run_id}` | body `review.triage` now present (see above) |
| `GET` | `/reviews` | each item adds `verdict`, `counts`, `filenames` (candidate first) |
| `GET` | `/reviews/{run_id}/issues` | adds `priority`, `area`, `thread_id` query filters; each issue body carries the new fields |
| `GET` | `/reviews/{run_id}/media/{asset_id}` | `{url, role, filename, content_type, kind}`; presigned GET for the player; `kind` = `video`/`audio`/`image` |
| `POST` | `/uploads` | validates `content_type` + extension: `original`/`candidate` must be video or audio; `reference` may also be `image/png`, `image/jpeg`, `image/webp`. 415 otherwise |

Reference images are stored and attached to the review (`media` list, role `reference`) but no evaluator
reads them yet; the dubbing evaluator ignores them.

CORS: `VREVIEW_CORS_ORIGINS` (comma-separated), default `http://localhost:53000`.

## 4. Web app (`web/`)

- Next.js (App Router, TypeScript), dev server on port **53000** (3000 is taken on this machine).
- better-auth with email + password and the JWT plugin; Postgres database `vreview_auth` on the existing
  compose Postgres (`localhost:55433`, user `vreview`). JWKS at `http://localhost:53000/api/auth/jwks`, which
  is what `VREVIEW_AUTH_JWKS_URL` points the API at. Issuer/audience: `http://localhost:53000`.
- The browser gets a JWT from better-auth and calls the FastAPI API (`NEXT_PUBLIC_VREVIEW_API_URL`,
  default `http://localhost:58000`) with `Authorization: Bearer`. Uploads go browser -> presigned
  multipart URLs -> MinIO/R2 directly.
- Pages: `/login`, `/` (reviews dashboard), `/reviews/new` (upload candidate + references, brief),
  `/reviews/[runId]` (synced player, timeline, threads, feedback).
