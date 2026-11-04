import { STAGES, stageIndex, stageLabel } from "@/lib/stages";
import type { RunStatus } from "@/lib/api/types";
import { cx } from "./common";

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
              "h-1 flex-1",
              i < idx ? "bg-interactive" : i === idx ? (failed ? "bg-blocker" : "bg-interactive/50 animate-pulse") : "bg-border",
            )}
          />
        ))}
      </span>
      <span className="text-xs text-muted-foreground">
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
    <ol className="border bg-card divide-y divide-border-subtle">
      {STAGES.map((s, i) => {
        const state =
          status === "failed" && i === idx ? "failed" : i < idx ? "done" : i === idx ? "running" : "pending";
        return (
          <li key={s.id} className="flex items-center gap-3 px-4 h-12" aria-current={state === "running" ? "step" : undefined}>
            <span
              className={cx(
                "size-5 rounded-full border inline-flex items-center justify-center text-[11px] font-semibold",
                state === "done" && "bg-interactive border-interactive text-primary-foreground",
                state === "running" && "border-interactive text-link",
                state === "failed" && "bg-blocker border-blocker text-primary-foreground",
                state === "pending" && "border-border text-muted-foreground",
              )}
              aria-hidden
            >
              {state === "done" ? "✓" : state === "failed" ? "!" : i + 1}
            </span>
            <span className={cx("flex-1", state === "pending" && "text-muted-foreground")}>{s.label}</span>
            <span className="text-xs text-muted-foreground">
              {state === "done" ? "Done" : state === "running" ? "Running…" : state === "failed" ? "Failed" : "Pending"}
            </span>
          </li>
        );
      })}
      {error && <li className="px-3 py-2 text-sm text-blocker font-mono">{error}</li>}
    </ol>
  );
}
