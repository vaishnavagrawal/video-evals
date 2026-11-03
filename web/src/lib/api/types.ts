// Types for the vreview /v1 API, following docs/triage-and-ui-contract.md and src/vreview/schema.py.

export type Severity = "critical" | "major" | "minor";
export type MediaRole = "original" | "candidate" | "reference";
export type Priority = "blocker" | "should-fix" | "needs-human" | "nit";
export type Area = "accuracy" | "sync" | "audio-mix" | "delivery" | "script" | "language" | "video" | "other";
export type Verdict = "request_changes" | "needs_human" | "approve";
export type TextSource = "rule" | "llm";
export type RunStatus = "queued" | "running" | "succeeded" | "failed";
export type FeedbackVerdict = "accept" | "false_positive" | "comment";

/** Display order (contract section 1: blocker > should-fix > needs-human > nit). */
export const PRIORITIES: Priority[] = ["blocker", "should-fix", "needs-human", "nit"];
export const PRIORITY_RANK: Record<Priority, number> = { blocker: 4, "should-fix": 3, "needs-human": 2, nit: 1 };

export type PriorityCounts = Record<Priority, number>;

export interface MediaRef {
  asset_id: string;
  role: MediaRole;
  key: string;
  sha256: string;
  filename?: string | null;
}

export interface Brief {
  kind?: "dub" | "generation";
  prompt?: string | null;
  script?: string | null;
  source_language?: string | null;
  target_languages?: string[];
  options?: Record<string, unknown>;
}

export interface Evidence {
  kind: "frame" | "clip" | "audio" | "text" | "metric";
  key?: string | null;
  t?: number | null;
  data?: Record<string, unknown>;
}

export interface VisualDetails {
  frames_analyzed?: number;
  face_frames?: number;
  max_face_area?: number;
  mouth_activity?: number;
  speaking_likely?: boolean | null;
}

export interface IssueDetails {
  source?: string;
  original_text?: string | null;
  dubbed_text?: string | null;
  visual?: VisualDetails | null;
  [k: string]: unknown;
}

export interface Issue {
  id: string;
  evaluator: string;
  category: string;
  severity: Severity;
  confidence: number;
  t0: number;
  t1: number;
  track?: string | null;
  shot_id?: string | null;
  description: string;
  fix_hint: string;
  suggested_prompt_patch?: string | null;
  regenerate_scope?: "shot" | "segment" | "full";
  evidence?: Evidence[];
  details: IssueDetails;
  // Triage additions (contract section 2). Defaults on the backend; optional here so old data parses.
  priority?: Priority;
  tags?: string[];
  area?: Area;
  thread_id?: string | null;
}

export interface EvaluatorResult {
  evaluator: string;
  version: string;
  status: "ok" | "skipped" | "failed";
  reason?: string | null;
  score?: number | null;
  issues: Issue[];
  summary: Record<string, unknown>;
  artifacts: Record<string, string>;
  warnings: string[];
  timings: Record<string, number>;
}

export interface Thread {
  id: string;
  track: string | null;
  t0: number;
  t1: number;
  priority: Priority;
  areas: Area[];
  issue_ids: string[];
  title: string;
  comment: string;
  comment_source: TextSource;
  /** Which LLM wrote title/comment when comment_source is "llm". */
  comment_model?: string | null;
  original_text?: string | null;
  dubbed_text?: string | null;
}

export interface Triage {
  version: string;
  verdict: Verdict;
  counts: PriorityCounts;
  area_counts: Partial<Record<Area, number>>;
  threads: Thread[];
  summary: string;
  summary_source: TextSource;
  summary_model?: string | null;
  llm_warnings: string[];
}

export interface Review {
  schema_version: string;
  run_id: string;
  brief: Brief;
  media: MediaRef[];
  results: EvaluatorResult[];
  score: number | null;
  created_at: string;
  triage?: Triage | null;
}

/** GET /v1/reviews/{run_id} */
export interface ReviewRun {
  run_id: string;
  status: RunStatus;
  stage: string | null;
  error: string | null;
  score: number | null;
  brief: Brief;
  evaluators: string[] | null;
  review: Review | null;
  hatchet_run_id?: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** One item of GET /v1/reviews */
export interface ReviewListItem {
  run_id: string;
  status: RunStatus;
  stage: string | null;
  score: number | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
  // Contract section 3 additions; null/absent while a run has no review yet.
  verdict?: Verdict | null;
  counts?: PriorityCounts | null;
  filenames?: string[];
}

export interface ReviewList {
  items: ReviewListItem[];
  total: number;
  limit: number;
  offset: number;
}

/** GET /v1/reviews/{run_id}/media/{asset_id} */
export interface MediaUrl {
  url: string;
  role: MediaRole;
  filename: string | null;
  content_type: string | null;
  kind: "video" | "audio" | "image";
}

/** POST /v1/uploads */
export interface CreateUploadRequest {
  filename: string;
  size: number;
  content_type?: string | null;
  role: MediaRole;
  project_id?: string | null;
}

export interface CreateUploadResponse {
  asset_id: string;
  upload_id: string;
  upload_key: string;
  part_size: number;
  parts: { part_number: number; url: string }[];
}

export interface CompletedPart {
  PartNumber: number;
  ETag: string;
}

/** POST /v1/reviews */
export interface CreateReviewRequest {
  assets: { asset_id: string; role: MediaRole }[];
  brief: Brief;
  evaluators?: string[] | null;
  webhook_url?: string | null;
  project_id?: string | null;
}

export interface CreateReviewResponse {
  run_id: string;
  status: RunStatus;
  hatchet_run_id?: string | null;
}

/** One row of GET /v1/reviews/{run_id}/feedback */
export interface FeedbackRow {
  issue_id: string;
  verdict: FeedbackVerdict;
  comment: string | null;
  created_at: string;
}

export interface FeedbackResponse {
  status: string;
  feedback_id: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Transport for one presigned part PUT. Resolves with the ETag header. */
export type PutPart = (
  url: string,
  body: Blob,
  opts: { onProgress: (loaded: number) => void; signal: AbortSignal },
) => Promise<string>;

export interface VreviewApi {
  listReviews(limit?: number, offset?: number): Promise<ReviewList>;
  getReview(runId: string): Promise<ReviewRun>;
  getMedia(runId: string, assetId: string): Promise<MediaUrl>;
  createUpload(req: CreateUploadRequest): Promise<CreateUploadResponse>;
  completeUpload(assetId: string, uploadId: string, parts: CompletedPart[]): Promise<void>;
  createReview(req: CreateReviewRequest): Promise<CreateReviewResponse>;
  sendFeedback(runId: string, issueId: string, verdict: FeedbackVerdict, comment?: string): Promise<FeedbackResponse>;
  /**
   * Every saved feedback row for a run, oldest first. Callers fold it: the last accept/false_positive per issue
   * is its verdict and every comment row is kept. (`?latest=true` is not used: it keeps one row per issue, so a
   * comment would hide the verdict.) Resolves to [] on 404 (older API without the endpoint).
   */
  listFeedback(runId: string): Promise<FeedbackRow[]>;
  /** Abort an unfinished multipart upload. Ignores 404 (older API, unknown asset) and 409 (already completed). */
  abortUpload(assetId: string): Promise<void>;
  putPart: PutPart;
}
