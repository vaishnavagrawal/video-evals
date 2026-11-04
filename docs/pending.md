# Pending work

Updated 2026-11-04. Open items only. What exists today: `docs/mvp-summary.md`. Live deployment: `deploy/coolify/README.md`. Triage and UI spec: `docs/triage-and-ui-contract.md`.

Priority: **P1** before real users, **P2** soon, **P3** when it hurts.

## Results are not repeatable (P1)

These decide whether a score can be trusted or used as a gate.

- **Judge model order.** `gemini-flash` is first in `llm_models`. On the same transcript it returned 0 issues in 3 of 3 runs, while `auto/best-fast` returned 8 to 11 (`dubeval/judge.py` moves to the second model only on error or timeout). Findings therefore depend on gateway latency. Options: put the stricter model first, judge with both and merge, or take the union of N runs.
- **Judge failures are warnings.** A chunk that fails on every model drops its issues and only adds a warning. A review with failed judge chunks should be marked incomplete, not scored as clean.
- **Run-to-run noise.** Demucs uses a random shift (`--shifts`, default 1), so stems differ by 35 to 45% RMS between runs; fix the seed or set shifts to 0. Groq Whisper returned 36 segments on a run where an earlier run of the same audio gave 45. A fresh rerun moved scores by about 2 points (es-v1 10.5 to 12.4, es-v2 9.1 to 6.8).
- **Mislabelled missing dub.** The judge sometimes returns `UNTRANSLATED` ("the dub is empty") for a missing dub, which adds a false `language` area to a thread.
- **Score vs verdict.** The score subtracts for every issue, the verdict only looks at blockers, so a 10 s clip scored 3.5/100 and was marked Approved. Explain it in the UI or change the score.
- **MediaPipe 0.10.35.** The FaceLandmarker flips `speaking_likely` on a few issues compared with 0.10.21, which moved 6 to 7 severities per report. Decide whether to accept it or tune thresholds.

## Security and operations (P1 unless noted)

- **Upload cap checks the declared size only.** A client can declare 1 byte and PUT up to 5 GiB into a part URL. Check the real object size in `complete` and delete the object if it is over the cap.
- **No rate limits or quotas.** Add per-tenant limits (video length, reviews per day) and cap LLM spend through Hatchet concurrency.
- **Shared credentials.** All services use one Postgres superuser, and the API and worker use the MinIO root key. Create per-service roles and a scoped S3 key.
- **Backups are not scheduled.** `deploy/coolify/README.md` has the `pg_dump` and volume commands for `postgres-data`, `minio-data` and `hatchet-config`. Put them on cron and test one restore.
- **Dead worker leaves a run `running` forever** (dev DB: `run_258c273815b1`, from 2026-10-31). Add a reconciler that fails runs whose Hatchet run is gone or older than a limit.
- **Dev API (`:8000`) and dev web (`:53000`) listen on all interfaces.** `web/.env.local` has `ALLOW_SIGNUP=1`, so anyone who can reach `:53000` can create an account on the dev stack. Bind both to `127.0.0.1` or close sign-up. (P2 once the firewall question is settled; the host has no `ufw` or Docker firewall rules.)
- **Host firewall.** `ufw` is inactive and `DOCKER-USER` is empty. Confirm whether the cloud provider has a firewall in front.
- **Secrets file permissions.** `.env` is mode 644 and holds the Hatchet token and LLM key. `chmod 600 .env`. (P2)
- **Env management is manual.** Variables are set directly in Coolify. Move them to a secrets manager with a machine identity. (P2)
- **No auto-deploy.** Pushing to `master` does not deploy; run `coolify deploy uuid <app-uuid>`. A GitHub webhook or a CI step would automate it. The Coolify GitHub App source returned HTTP 500 when listing repositories, so the app uses a read-only deploy key.
- **Hatchet dashboard is not exposed.** To use it, add a domain for `hatchet` on `8888` and set `SERVER_URL`; tokens embed it, so create a new token afterwards. `hatchet-lite` is meant for dev and low volume; move to the split engine when volume grows. (P3)
- **First deploy of a fresh stack** restarts the worker until `HATCHET_CLIENT_TOKEN` is set. Documented in the runbook.
- **Cold start.** Whisper, Demucs and MediaPipe download on the first review. They are cached in the `worker-data` volume; bake them into the image if cold starts matter. (P3)
- **Abandoned multipart uploads** (cancel when abort fails, closed tabs) stay in MinIO. Add a bucket lifecycle rule. (P3)

## Correctness and cleanup (P2)

- **Artifact names.** `review.results[].artifacts` lists `hi->es`, but the object is `reviews/<run>/dubbing/hi->es.json`, and `GET /v1/reviews/{run}/artifacts/{name}` resolves `name` after `reviews/<run>/`. The working name is `dubbing/hi-%3Ees.json`. Make them agree.
- **Ingest validation.** Media with no usable audio or duration fails later with `v cannot be empty`. Reject it up front with a clear error.
- **LLM review summary** is accurate but formulaic ("driven by 2 blocker threads…"). Tighten the prompt in `triage_llm.py`.
- **Unused settings.** `DUBEVAL_LLM_MODEL` is passed to the worker but nothing reads it, and `DUBEVAL_ASR_*` is not wired in the Coolify compose. The `auth` and `dotenv-cli` devDependencies in `web/package.json` are no longer used.
- **Dev DB leftovers.** `run_01843ba87dc7` has probe feedback rows from UI testing. `/tmp/env.bak.20261103` is the previous local `.env` (now mode 600); delete it once the new auth values are confirmed.

## Product roadmap (from `docs/mvp-summary.md`)

1. **Golden sets and a precision/recall harness**, with a CI gate per evaluator version. Start exporting `issue_feedback` by evaluator version now. This is the prerequisite for judging items 3 to 5, and for the judge-order decision above.
2. **GPU offload** of `track_features` (ASR, Demucs) to Modal. It depends only on its arguments, so it moves as is.
3. **Evaluator #2:** identity and temporal consistency (shot detection, face/embedding drift, VLM judge).
4. **Review agent** (pydantic-ai): picks evaluators from the brief, investigation tools, re-review diffing between versions. Runs inside a single Hatchet task; keep its calls cached.
5. **Reference images** are stored and shown but no evaluator reads them. Needs an evaluator that uses them.
