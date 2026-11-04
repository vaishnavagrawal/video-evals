"use client";
import Link from "next/link";
import { api, ApiError, type ReviewRun } from "@/lib/api";
import { usePoll } from "@/lib/use-poll";
import { ArrowLeftIcon } from "@phosphor-icons/react";
import { Button } from "@/components/ui/button";
import { StageList } from "../stage-progress";
import { PipelineSection, PipelineView } from "../pipeline-view";
import { ErrorBox, Spinner, StatusBadge, fmtDate } from "../common";
import { ReviewDetail } from "./review-detail";

const inFlight = (r: ReviewRun) => r.status === "queued" || r.status === "running";

export function ReviewPage({ runId: id }: { runId: string }) {
  const { data, error } = usePoll(() => api.getReview(id), inFlight, 3000);

  if (error && !data) {
    return (
      <div className="px-4 py-6 lg:px-6 space-y-3">
        <Button asChild variant="ghost" size="sm" className="-ml-2">
          <Link href="/">
            <ArrowLeftIcon data-icon="inline-start" />
            Reviews
          </Link>
        </Button>
        <ErrorBox>
          {error instanceof ApiError && error.status === 404 ? "Review not found." : `Could not load the review: ${error.message}`}
        </ErrorBox>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="px-4 py-6 lg:px-6">
        <Spinner label="Loading review" />
      </div>
    );
  }

  if (data.status === "succeeded" && data.review)
    return <ReviewDetail review={data.review} pipeline={<PipelineSection runId={data.run_id} />} />;

  return (
    <div className="w-full max-w-[980px] px-4 py-6 lg:px-6">
      <div className="flex items-center gap-3 mb-1">
        <h1 className="text-2xl font-normal font-mono">{data.run_id}</h1>
        <StatusBadge status={data.status} />
      </div>
      <p className="text-muted-foreground mb-6 text-sm">
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
        <PipelineView
          runId={data.run_id}
          error={data.status === "failed" ? data.error : null}
          fallback={<StageList status={data.status} stage={data.stage} error={data.error} />}
        />
      </div>
    </div>
  );
}
