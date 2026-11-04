"use client";
// Mock vreview API (NEXT_PUBLIC_VREVIEW_MOCK=1). Built from the real es-v1 EvaluatorResult fixture with
// triage synthesized by the contract rules (src/lib/triage.ts). Same interface as the real client.
import fixture from "@/mocks/es-v1-result.json";
import { STAGES } from "@/lib/stages";
import { triage } from "@/lib/triage";
import {
  ApiError,
  type Brief,
  type CreateReviewRequest,
  type EvaluatorResult,
  type FeedbackRow,
  type MediaRef,
  type MediaUrl,
  type Pipeline,
  type PipelineNode,
  type PutPart,
  type Review,
  type ReviewListItem,
  type ReviewRun,
  type RunStatus,
  type VreviewApi,
} from "./types";

const MOCK_PART_SIZE = 8 * 1024 * 1024;
const STAGE_MS = 4000;
const STORE_KEY = "vreview-mock-runs";
const MODULE_START = Date.now();

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));

const FIXTURE_MEDIA: MediaRef[] = [
  { asset_id: "ast_mock_candidate", role: "candidate", key: "media/mock-candidate.mp4", sha256: "", filename: "dub-01.mp4" },
  { asset_id: "ast_mock_original", role: "original", key: "media/mock-original.mp4", sha256: "", filename: "original.mp4" },
  { asset_id: "ast_mock_reference", role: "reference", key: "media/mock-reference.jpg", sha256: "", filename: "key-frame.jpg" },
];

const MEDIA_URLS: Record<string, Omit<MediaUrl, "role" | "filename">> = {
  ast_mock_original: { url: "/api/mock-media/original", content_type: "video/mp4", kind: "video" },
  ast_mock_candidate: { url: "/api/mock-media/candidate", content_type: "video/mp4", kind: "video" },
  ast_mock_reference: { url: "/mock/reference-frame.jpg", content_type: "image/jpeg", kind: "image" },
};

function buildReview(runId: string, createdAt: string, brief: Brief, media: MediaRef[], llm = false): Review {
  const result = structuredClone(fixture) as unknown as EvaluatorResult;
  const review: Review = {
    schema_version: "vreview.v1",
    run_id: runId,
    brief,
    media,
    results: [result],
    score: result.score ?? null,
    created_at: createdAt,
  };
  review.triage = triage(review);
  if (llm && review.triage) {
    // Hand-written stand-in for LLM output so the UI's "LLM-written" markers can be exercised.
    review.triage.summary_source = "llm";
    review.triage.summary_model = "mock-llm";
    review.triage.summary =
      "The Spanish dub is **not ready to ship**. Timing is the main problem: many lines start early or are cut " +
      "off while the actor is still speaking, and about ten original lines have no dub at all. A few translations " +
      "change the meaning of the line. Fix the blockers first, then re-time the overrunning lines.";
    for (const t of review.triage.threads.slice(0, 3)) {
      t.comment_source = "llm";
      t.comment_model = "mock-llm";
      t.comment =
        `The dub and the original disagree here (${t.issue_ids.length} finding${t.issue_ids.length > 1 ? "s" : ""}).\n\n` +
        "**Why it matters:** viewers notice when the voice stops before the lips do.\n\n" +
        "**Fix:** re-time the dubbed line to the original onset and offset, or regenerate it shorter.";
    }
  }
  return review;
}

interface StoredRun {
  run_id: string;
  created: number;
  brief: Brief;
  assets: CreateReviewRequest["assets"];
  filenames: string[];
}

function loadStored(): StoredRun[] {
  if (typeof window === "undefined") return [];
  try {
    return JSON.parse(sessionStorage.getItem(STORE_KEY) || "[]");
  } catch {
    return [];
  }
}
function saveStored(runs: StoredRun[]) {
  sessionStorage.setItem(STORE_KEY, JSON.stringify(runs));
}

const ES_BRIEF: Brief = { kind: "dub", source_language: "hi", target_languages: ["es"] };
const FIXED_CREATED = "2026-11-02T19:40:12Z";
let fixedReview: Review | null = null;
let fixedLlmReview: Review | null = null;
const getFixed = () => (fixedReview ??= buildReview("rerun-es-v1", FIXED_CREATED, ES_BRIEF, FIXTURE_MEDIA));
const getFixedLlm = () =>
  (fixedLlmReview ??= buildReview("rerun-es-v1-llm", "2026-11-02T21:05:40Z", ES_BRIEF, FIXTURE_MEDIA, true));

function runFrom(
  run_id: string,
  status: RunStatus,
  stage: string | null,
  created_at: string,
  brief: Brief,
  review: Review | null,
  error: string | null = null,
): ReviewRun {
  return {
    run_id,
    status,
    stage,
    error,
    score: review?.score ?? null,
    brief,
    evaluators: ["dubbing"],
    review,
    hatchet_run_id: null,
    created_at,
    updated_at: created_at,
  };
}

/** Elapsed-time state machine for runs created in this browser session: queued -> each stage -> succeeded. */
function storedRun(s: StoredRun): ReviewRun {
  const created = new Date(s.created).toISOString();
  const step = Math.floor((Date.now() - s.created) / STAGE_MS);
  if (step === 0) return runFrom(s.run_id, "queued", null, created, s.brief, null);
  if (step <= STAGES.length) return runFrom(s.run_id, "running", STAGES[step - 1].id, created, s.brief, null);
  const byId = new Map(FIXTURE_MEDIA.map((m) => [m.asset_id, m]));
  // The fixture media stands in for the uploaded files so the review page has something to play.
  const media = s.assets.map((a, i) => ({
    ...(a.role === "candidate"
      ? byId.get("ast_mock_candidate")!
      : a.role === "original"
        ? byId.get("ast_mock_original")!
        : byId.get("ast_mock_reference")!),
    filename: s.filenames[i],
  }));
  return runFrom(s.run_id, "succeeded", "completed", created, s.brief, buildReview(s.run_id, created, s.brief, media));
}

function allRuns(): ReviewRun[] {
  const runningStage = STAGES[Math.min(2 + Math.floor((Date.now() - MODULE_START) / 15000), 3)].id;
  const fixed: ReviewRun[] = [
    runFrom("run_mock_running", "running", runningStage, "2026-11-03T10:58:02Z", { ...ES_BRIEF }, null),
    runFrom("run_mock_queued", "queued", null, "2026-11-03T11:01:44Z", { kind: "dub", target_languages: [] }, null),
    runFrom("rerun-es-v1-llm", "succeeded", "completed", "2026-11-02T21:05:40Z", ES_BRIEF, getFixedLlm()),
    runFrom("rerun-es-v1", "succeeded", "completed", FIXED_CREATED, ES_BRIEF, getFixed()),
    runFrom(
      "run_mock_failed",
      "failed",
      "dubbing_features",
      "2026-11-01T16:22:09Z",
      { kind: "dub", source_language: "en", target_languages: ["de"] },
      null,
      "dubbing_features: v cannot be empty (the clip has no usable audio)",
    ),
  ];
  return [...loadStored().map(storedRun).reverse(), ...fixed];
}

const FIXED_FILENAMES: Record<string, string[]> = {
  run_mock_running: ["trailer-es.mp4", "trailer.mp4"],
  run_mock_queued: ["episode-04-pt.mov", "episode-04.mov"],
  "rerun-es-v1-llm": ["dub-01.mp4", "original.mp4", "key-frame.jpg"],
  "rerun-es-v1": ["dub-01.mp4", "original.mp4", "key-frame.jpg"],
  run_mock_failed: ["clip-de.mp4", "clip.mp4"],
};

function toListItem(r: ReviewRun): ReviewListItem {
  const stored = loadStored().find((s) => s.run_id === r.run_id);
  return {
    run_id: r.run_id,
    status: r.status,
    stage: r.stage,
    score: r.score,
    error: r.error,
    created_at: r.created_at,
    updated_at: r.updated_at,
    verdict: r.review?.triage?.verdict ?? null,
    counts: r.review?.triage?.counts ?? null,
    filenames: stored?.filenames ?? FIXED_FILENAMES[r.run_id] ?? [],
  };
}

const uploads = new Map<string, { filename: string; role: string; parts: number; aborted?: boolean }>();
const FEEDBACK_KEY = "vreview-mock-feedback";
const loadFeedback = (): Record<string, FeedbackRow[]> => {
  try {
    return JSON.parse(sessionStorage.getItem(FEEDBACK_KEY) || "{}");
  } catch {
    return {};
  }
};
const failedOnce = new Set<string>();

/** Simulated part PUT: ~40 MB/s, and the first attempt of part 2 of each upload fails so retry is exercised. */
const mockPutPart: PutPart = (url, body, { onProgress, signal }) =>
  new Promise((resolve, reject) => {
    const total = body.size;
    const ms = Math.max(400, (total / (40 * 1024 * 1024)) * 1000);
    const shouldFail = /\/2$/.test(url) && !failedOnce.has(url);
    let elapsed = 0;
    const timer = setInterval(() => {
      elapsed += 100;
      const frac = Math.min(1, elapsed / ms);
      onProgress(Math.round(total * frac));
      if (shouldFail && frac >= 0.5) {
        clearInterval(timer);
        failedOnce.add(url);
        reject(new Error("Simulated network error (mock)"));
      } else if (frac >= 1) {
        clearInterval(timer);
        resolve(`"mock-etag-${url.split("/").slice(-2).join("-")}"`);
      }
    }, 100);
    signal.addEventListener(
      "abort",
      () => {
        clearInterval(timer);
        reject(new DOMException("Upload cancelled", "AbortError"));
      },
      { once: true },
    );
  });

let seq = 0;
const genId = (p: string) => `${p}_mock${Date.now().toString(36)}${(seq++).toString(36)}`;

// --- Pipeline (GET /reviews/{id}/pipeline) ---------------------------------------------------------------------
// Synthesized from the same run states as above, with the node ids, labels and detail keys the real worker emits
// (src/vreview/workflows/review.py), so the pipeline view can be exercised without a backend.

const ASR_MODEL = "groq/whisper-large-v3";
const JUDGE_MODELS = ["meta:muse-spark-1.3", "gemini-flash"];

interface MockChild {
  node: string;
  label: string;
  /** Start and end as fractions of the parent's duration. */
  from: number;
  to: number;
  detail: Record<string, unknown>;
  /** Added once the child has finished. */
  done?: Record<string, unknown>;
  error?: string;
}

interface MockStep {
  node: string;
  label: string;
  ms: number;
  detail?: Record<string, unknown>;
  /** Added once the step has finished. */
  done?: Record<string, unknown>;
  children?: MockChild[];
  error?: string;
}

/** Steps of a dubbing review. `secs` are realistic durations for a ~1 minute clip; `cached` is a re-run. */
function mockSteps(review: Review | null, opts: { cached?: boolean; avJudge?: boolean; secs?: number[] }): MockStep[] {
  const cached = !!opts.cached;
  const secs = opts.secs ?? (cached ? [5, 3, 4, 9, 2, 31] : [6, 14, 96, 142, 2, 26]);
  const issues = review?.results.reduce((n, r) => n + r.issues.length, 0) ?? 42;
  const score = review?.score != null ? Math.round(review.score * 10) / 10 : 71.3;
  const tri = review?.triage;
  const llm = tri?.summary_source === "llm";
  const ms = (i: number) => secs[i] * 1000;
  const steps: MockStep[] = [
    { node: "ingest", label: "Ingest media", ms: ms(0), done: { files: review?.media.length ?? 3 } },
    {
      node: "dubbing_plan",
      label: "Plan audio tracks",
      ms: ms(1),
      done: { tracks: 2, source: "hi", targets: ["es"] },
    },
    {
      node: "dubbing_features",
      label: "Extract speech features",
      ms: ms(2),
      detail: { model: ASR_MODEL },
      done: { tracks: 2 },
      children: [
        {
          node: "dubbing_features/3f9c1a7be2d40a15.a0",
          label: "Original · hi",
          from: 0.01,
          to: cached ? 0.06 : 0.97,
          detail: { lang: "hi", model: ASR_MODEL },
          done: { cached },
        },
        {
          node: "dubbing_features/b81d52c0e9a7f364.a0",
          label: "Dub · es",
          from: 0.01,
          to: cached ? 0.05 : 0.64,
          detail: { lang: "es", model: ASR_MODEL },
          done: { cached },
        },
      ],
    },
    {
      node: "dubbing_pairs",
      label: "Evaluate dubbed tracks",
      ms: ms(3),
      detail: { models: JUDGE_MODELS, visual: true },
      done: { pairs: 1, issues, score },
      children: [
        {
          node: "dubbing_pairs/b81d52c0e9a7f364.a0",
          label: "Compare hi → es",
          from: 0.01,
          to: 0.99,
          detail: { pair: "hi → es", models: JUDGE_MODELS, visual: true },
          done: { cached, score, issues },
        },
      ],
    },
    { node: "assemble", label: "Assemble results", ms: ms(4), done: { evaluators: ["dubbing"], issues, score } },
  ];
  if (opts.avJudge) {
    // Not part of STAGES: shows that the view draws whatever nodes the server returns.
    steps.push({
      node: "av_judge",
      label: "AV judge (video + audio)",
      ms: 38_000,
      detail: { model: JUDGE_MODELS[0] },
      done: { issues: 4, cached },
    });
  }
  steps.push({
    node: "finalize",
    label: "Triage & finalize",
    ms: ms(5),
    done: {
      issues,
      score,
      verdict: tri?.verdict ?? "request_changes",
      threads: tri?.threads.length ?? 12,
      ...(llm ? { models: [tri?.summary_model ?? "mock-llm"] } : {}),
    },
  });
  return steps;
}

/** Lays steps end to end from `startMs` and derives each node's status from `nowMs`. A step with `error` fails. */
function mockTimeline(steps: MockStep[], startMs: number, nowMs: number): PipelineNode[] {
  const iso = (t: number) => new Date(t).toISOString();
  const out: PipelineNode[] = [];
  const push = (n: {
    node: string;
    parent: string | null;
    label: string;
    start: number;
    end: number;
    detail: Record<string, unknown>;
    done?: Record<string, unknown>;
    error?: string;
  }) => {
    const finished = n.end <= nowMs;
    const status = !finished ? "running" : n.error ? "failed" : "succeeded";
    out.push({
      node: n.node,
      parent: n.parent,
      label: n.label,
      kind: n.parent ? "child" : "stage",
      status,
      started_at: iso(n.start),
      finished_at: finished ? iso(n.end) : null,
      duration_s: Math.round(((finished ? n.end : nowMs) - n.start)) / 1000,
      detail: { ...n.detail, ...(finished && !n.error ? n.done : {}), ...(finished && n.error ? { error: n.error } : {}) },
    });
  };

  let at = startMs;
  for (const s of steps) {
    if (at > nowMs) break;
    const end = at + s.ms;
    push({ node: s.node, parent: null, label: s.label, start: at, end, detail: s.detail ?? {}, done: s.done, error: s.error });
    for (const c of s.children ?? []) {
      const cs = at + c.from * s.ms;
      if (cs > nowMs) continue;
      push({
        node: c.node,
        parent: s.node,
        label: c.label,
        start: cs,
        end: at + c.to * s.ms,
        detail: c.detail,
        done: c.done,
        error: c.error,
      });
    }
    if (s.error) break;
    at = end;
  }
  return out;
}

function mockPipeline(run: ReviewRun): Pipeline {
  const now = Date.now();
  const created = new Date(run.created_at ?? now).getTime();
  const wrap = (nodes: PipelineNode[], at = now): Pipeline => ({
    run_id: run.run_id,
    status: run.status,
    stage: run.stage,
    server_time: new Date(at).toISOString(),
    nodes,
  });

  const stored = loadStored().find((s) => s.run_id === run.run_id);
  if (stored) {
    // Same clock as storedRun: stage i runs from created + (i + 1) * STAGE_MS for STAGE_MS.
    const steps = mockSteps(run.review, { secs: STAGES.map(() => STAGE_MS / 1000) });
    return wrap(mockTimeline(steps, created + STAGE_MS, now));
  }

  if (run.run_id === "run_mock_running") {
    const idx = Math.max(0, STAGES.findIndex((s) => s.id === run.stage));
    const steps = mockSteps(null, {});
    const elapsed = ((now - MODULE_START) % 15000) + 4000;
    const before = steps.slice(0, idx).reduce((t, s) => t + s.ms, 0);
    return wrap(mockTimeline(steps, now - before - elapsed, now));
  }

  if (run.run_id === "run_mock_failed") {
    const failedAt = Math.max(now, created + 90_000); // the run is over, whatever this browser's clock says
    const steps = mockSteps(null, {}).slice(0, 3);
    steps[2] = {
      ...steps[2],
      ms: 31_000,
      error: "v cannot be empty (the clip has no usable audio)",
      children: [
        {
          ...steps[2].children![0],
          label: "Original · en",
          to: 0.9,
          detail: { lang: "en", model: ASR_MODEL },
          error: "v cannot be empty (the clip has no usable audio)",
        },
        { ...steps[2].children![1], label: "Dub · de", to: 0.7, detail: { lang: "de", model: ASR_MODEL } },
      ],
    };
    return wrap(mockTimeline(steps, created, failedAt), failedAt);
  }

  if (run.status === "succeeded") {
    const cached = run.run_id === "rerun-es-v1-llm";
    const steps = mockSteps(run.review, { cached, avJudge: true });
    const total = steps.reduce((t, s) => t + s.ms, 0);
    return wrap(mockTimeline(steps, created, created + total + 1000), created + total + 1000);
  }

  return wrap([]); // queued
}

export const mockApi: VreviewApi = {
  async listReviews(limit = 50, offset = 0) {
    await delay(150);
    const runs = allRuns();
    return { items: runs.slice(offset, offset + limit).map(toListItem), total: runs.length, limit, offset };
  },
  async getReview(runId) {
    await delay(150);
    const r = allRuns().find((x) => x.run_id === runId);
    if (!r) throw new ApiError(404, "Review run not found");
    return r;
  },
  async getPipeline(runId) {
    await delay(100);
    const r = allRuns().find((x) => x.run_id === runId);
    if (!r) throw new ApiError(404, "Review run not found");
    return mockPipeline(r);
  },
  async getMedia(runId, assetId) {
    await delay(50);
    const r = allRuns().find((x) => x.run_id === runId);
    const ref = r?.review?.media.find((m) => m.asset_id === assetId);
    const u = MEDIA_URLS[assetId];
    if (!ref || !u) throw new ApiError(404, "Media not found");
    return { ...u, role: ref.role, filename: ref.filename ?? null };
  },
  async createUpload(req) {
    await delay(120);
    const asset_id = genId("ast");
    const n = Math.max(1, Math.ceil(req.size / MOCK_PART_SIZE));
    uploads.set(asset_id, { filename: req.filename, role: req.role, parts: n });
    return {
      asset_id,
      upload_id: genId("upl"),
      upload_key: `uploads/${asset_id}/${req.filename}`,
      part_size: MOCK_PART_SIZE,
      parts: Array.from({ length: n }, (_, i) => ({ part_number: i + 1, url: `mock://upload/${asset_id}/${i + 1}` })),
    };
  },
  async completeUpload(assetId, _uploadId, parts) {
    await delay(150);
    const u = uploads.get(assetId);
    if (!u) throw new ApiError(404, "Asset not found");
    if (parts.length !== u.parts) throw new ApiError(400, `Expected ${u.parts} parts, got ${parts.length}`);
  },
  async createReview(req) {
    await delay(200);
    if (!req.assets.some((a) => a.role === "candidate")) throw new ApiError(400, "A candidate asset is required");
    const run_id = genId("run");
    const filenames = req.assets.map((a) => uploads.get(a.asset_id)?.filename ?? a.asset_id);
    saveStored([...loadStored(), { run_id, created: Date.now(), brief: req.brief, assets: req.assets, filenames }]);
    return { run_id, status: "queued", hatchet_run_id: null };
  },
  async sendFeedback(runId, issueId, verdict, comment) {
    await delay(250);
    const r = allRuns().find((x) => x.run_id === runId);
    if (!r?.review?.results.some((res) => res.issues.some((i) => i.id === issueId)))
      throw new ApiError(404, "Issue not found");
    const all = loadFeedback();
    all[runId] = [...(all[runId] ?? []), { issue_id: issueId, verdict, comment: comment ?? null, created_at: new Date().toISOString() }];
    sessionStorage.setItem(FEEDBACK_KEY, JSON.stringify(all));
    return { status: "ok", feedback_id: genId("fb") };
  },
  async listFeedback(runId) {
    await delay(100);
    // Full history, oldest first, like GET /feedback without ?latest.
    return loadFeedback()[runId] ?? [];
  },
  async abortUpload(assetId) {
    await delay(50);
    const u = uploads.get(assetId);
    if (u) u.aborted = true;
    console.info(`[mock] abortUpload ${assetId}`);
  },
  putPart: mockPutPart,
};
