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
