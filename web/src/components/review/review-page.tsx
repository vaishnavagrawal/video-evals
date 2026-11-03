"use client";
import Link from "next/link";
import { api, ApiError, type ReviewRun } from "@/lib/api";
import { usePoll } from "@/lib/use-poll";
import { StageList } from "../stage-progress";
import { ErrorBox, Spinner, StatusBadge, fmtDate } from "../ui";
import { ReviewDetail } from "./review-detail";

const inFlight = (r: ReviewRun) => r.status === "queued" || r.status === "running";

export function ReviewPage({ runId: id }: { runId: string }) {
  const { data, error } = usePoll(() => api.getReview(id), inFlight, 3000);

  if (error && !data) {
    return (
      <div className="mx-auto max-w-[1440px] px-4 py-5 space-y-3">
        <Link href="/" className="text-accent text-[13px] hover:underline">
          ← Reviews
        </Link>
        <ErrorBox>
          {error instanceof ApiError && error.status === 404 ? "Review not found." : `Could not load the review: ${error.message}`}
        </ErrorBox>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="mx-auto max-w-[1440px] px-4 py-5">
        <Spinner label="Loading review" />
      </div>
    );
  }

  if (data.status === "succeeded" && data.review) return <ReviewDetail review={data.review} />;

  return (
    <div className="mx-auto max-w-[760px] px-4 py-5">
      <Link href="/" className="text-accent text-[13px] hover:underline">
        ← Reviews
      </Link>
      <div className="flex items-center gap-3 mt-2 mb-1">
        <h1 className="text-[18px] font-semibold font-mono">{data.run_id}</h1>
        <StatusBadge status={data.status} />
      </div>
      <p className="text-fg-muted mb-4 text-[13px]">
        Created {fmtDate(data.created_at)}
        {inFlight(data) && " · this page updates every few seconds"}
      </p>
      <div aria-live="polite">
        {data.status === "failed" ? (
          <p className="mb-3">The review failed. The step that failed is marked below.</p>
        ) : data.status === "queued" ? (
          <p className="mb-3">Queued. A worker will pick it up shortly.</p>
        ) : (
          <p className="mb-3">Reviewing. A 1-minute clip takes about 2–3 minutes; full-length videos take longer.</p>
        )}
        <StageList status={data.status} stage={data.stage} error={data.error} />
      </div>
    </div>
  );
}
