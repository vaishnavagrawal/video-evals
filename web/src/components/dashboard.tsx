"use client";
import Link from "next/link";
import { api, type ReviewList } from "@/lib/api";
import { usePoll } from "@/lib/use-poll";
import { StageBar } from "./stage-progress";
import {
  ErrorBox,
  PriorityCountsInline,
  Spinner,
  StatusBadge,
  VerdictBadge,
  btn,
  cx,
  fmtDate,
} from "./ui";

const active = (l: ReviewList) => l.items.some((i) => i.status === "queued" || i.status === "running");

export function Dashboard() {
  const { data, error } = usePoll(() => api.listReviews(50, 0), active, 3000);

  return (
    <div className="mx-auto max-w-[1440px] px-4 py-5">
      <div className="flex items-center gap-3 mb-3">
        <h1 className="text-[18px] font-semibold">Reviews</h1>
        {data && <span className="text-fg-muted text-[13px]">{data.total} total</span>}
        {data && active(data) && (
          <span className="text-[12px] text-fg-muted inline-flex items-center gap-1.5">
            <span className="size-1.5 rounded-full bg-accent animate-pulse" aria-hidden />
            Updating live
          </span>
        )}
        <Link href="/reviews/new" className={cx(btn.base, btn.primary, "ml-auto")}>
          New review
        </Link>
      </div>

      {error && !data && <ErrorBox>Could not load reviews: {error.message}</ErrorBox>}
      {!data && !error && <Spinner label="Loading reviews" />}

      {data && data.items.length === 0 && (
        <div className="rounded-md border border-dashed border-line py-16 text-center">
          <h2 className="text-[16px] font-semibold mb-1">No reviews yet</h2>
          <p className="text-fg-muted mb-4">Upload a dubbed video and its original to get a review with timestamped comments.</p>
          <Link href="/reviews/new" className={cx(btn.base, btn.primary)}>
            Upload a video
          </Link>
        </div>
      )}

      {data && data.items.length > 0 && (
        <div className="rounded-md border border-line overflow-x-auto">
          <table className="w-full text-[13px] border-collapse">
            <caption className="sr-only">Reviews, newest first</caption>
            <thead className="bg-subtle text-fg-muted text-[12px]">
              <tr className="text-left">
                <th scope="col" className="font-medium px-3 h-9">Review</th>
                <th scope="col" className="font-medium px-3">Status</th>
                <th scope="col" className="font-medium px-3">Verdict</th>
                <th scope="col" className="font-medium px-3 text-right">Score</th>
                <th scope="col" className="font-medium px-3">
                  <span title="Blocker / should fix / needs human / nit">Threads</span>
                  <span className="sr-only"> (blocker, should fix, needs human, nit)</span>
                </th>
                <th scope="col" className="font-medium px-3">Created</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((r) => {
                const [candidate, ...rest] = r.filenames ?? [];
                return (
                  <tr key={r.run_id} className="border-t border-line-soft hover:bg-subtle/60 align-top">
                    <td className="px-3 py-2">
                      <Link href={`/reviews/${encodeURIComponent(r.run_id)}`} className="font-medium text-fg hover:text-accent hover:underline">
                        {candidate ?? r.run_id}
                      </Link>
                      <div className="text-[12px] text-fg-muted truncate max-w-[200px] xl:max-w-[360px]">
                        {rest.length ? <>vs {rest.join(", ")} · </> : null}
                        <span className="font-mono">{r.run_id}</span>
                      </div>
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex flex-col items-start gap-1.5 2xl:flex-row 2xl:gap-3">
                        <StatusBadge status={r.status} />
                        {(r.status === "running" || r.status === "queued" || r.status === "failed") && (
                          <StageBar status={r.status} stage={r.stage} />
                        )}
                      </div>
                      {r.status === "failed" && r.error && (
                        <div className="mt-1 text-[12px] text-blocker max-w-[210px] xl:max-w-[300px] truncate" title={r.error}>
                          {r.error}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <VerdictBadge verdict={r.verdict} />
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">
                      {r.score == null ? <span className="text-fg-muted">–</span> : r.score.toFixed(1)}
                    </td>
                    <td className="px-3 py-2">
                      <PriorityCountsInline counts={r.counts} />
                    </td>
                    <td className="px-3 py-2 text-fg-muted whitespace-nowrap">
                      <time dateTime={r.created_at ?? undefined}>{fmtDate(r.created_at)}</time>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
