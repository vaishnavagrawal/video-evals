import { STAGES, stageIndex, stageLabel } from "@/lib/stages";
import type { RunStatus } from "@/lib/api/types";
import { cx } from "./ui";

/** Compact segmented bar: one segment per workflow stage. */
export function StageBar({ status, stage }: { status: RunStatus; stage: string | null }) {
  const idx = status === "queued" ? -1 : stageIndex(stage);
  const failed = status === "failed";
  return (
    <span className="inline-flex flex-col gap-1 min-w-[150px] max-w-[210px]">
      <span className="flex gap-0.5" aria-hidden>
        {STAGES.map((s, i) => (
          <span
            key={s.id}
            className={cx(
              "h-1 flex-1 rounded-sm",
              i < idx ? "bg-accent" : i === idx ? (failed ? "bg-blocker" : "bg-accent/50 animate-pulse") : "bg-line",
            )}
          />
        ))}
      </span>
      <span className="text-[12px] text-fg-muted">
        {status === "queued"
          ? "Waiting for a worker"
          : `${failed ? "Failed at" : "Step"} ${Math.max(idx, 0) + 1}/${STAGES.length}: ${stageLabel(stage)}`}
      </span>
    </span>
  );
}

/** Full stage list for a running review. */
export function StageList({ status, stage, error }: { status: RunStatus; stage: string | null; error: string | null }) {
  const idx = status === "queued" ? -1 : stageIndex(stage);
  return (
    <ol className="space-y-0 border border-line rounded-md divide-y divide-line-soft">
      {STAGES.map((s, i) => {
        const state =
          status === "failed" && i === idx ? "failed" : i < idx ? "done" : i === idx ? "running" : "pending";
        return (
          <li key={s.id} className="flex items-center gap-3 px-3 h-10" aria-current={state === "running" ? "step" : undefined}>
            <span
              className={cx(
                "size-5 rounded-full border inline-flex items-center justify-center text-[11px] font-semibold",
                state === "done" && "bg-accent border-accent text-white",
                state === "running" && "border-accent text-accent",
                state === "failed" && "bg-blocker border-blocker text-white",
                state === "pending" && "border-line text-fg-muted",
              )}
              aria-hidden
            >
              {state === "done" ? "✓" : state === "failed" ? "!" : i + 1}
            </span>
            <span className={cx("flex-1", state === "pending" && "text-fg-muted")}>{s.label}</span>
            <span className="text-[12px] text-fg-muted">
              {state === "done" ? "Done" : state === "running" ? "Running…" : state === "failed" ? "Failed" : "Pending"}
            </span>
          </li>
        );
      })}
      {error && <li className="px-3 py-2 text-[13px] text-blocker font-mono">{error}</li>}
    </ol>
  );
}
