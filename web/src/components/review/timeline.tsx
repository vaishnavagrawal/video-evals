"use client";
import { useMemo } from "react";
import { PRIORITIES, type Priority, type Thread } from "@/lib/api/types";
import { PRIORITY_META, cx, fmtTime } from "../common";
import { useSync, type SyncController } from "./sync";

const LANE_H = 18;
const LABEL_W = 104;

function tickStep(d: number) {
  const target = d / 10;
  return [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800].find((s) => s >= target) ?? 3600;
}

function Playhead({ ctl, duration }: { ctl: SyncController; duration: number }) {
  const t = useSync(ctl, (s) => s.t);
  const pct = duration ? Math.min(100, (100 * t) / duration) : 0;
  return (
    <div
      aria-hidden
      className="absolute top-0 bottom-0 w-[2px] -ml-px bg-foreground pointer-events-none"
      style={{ left: `${pct}%` }}
    >
      <div className="absolute -top-1 -left-[3px] size-2 rotate-45 bg-foreground" />
    </div>
  );
}

export function Timeline({
  threads,
  visible,
  selectedId,
  duration,
  ctl,
  onSelect,
}: {
  threads: Thread[];
  visible: Set<string>;
  selectedId: string | null;
  duration: number;
  ctl: SyncController;
  onSelect: (id: string) => void;
}) {
  const byLane = useMemo(() => {
    const m: Record<Priority, Thread[]> = { blocker: [], "should-fix": [], "needs-human": [], nit: [] };
    for (const t of threads) m[t.priority].push(t);
    return m;
  }, [threads]);
  const D = duration || 1;
  const step = tickStep(D);
  const ticks: number[] = [];
  for (let s = 0; s <= D; s += step) ticks.push(s);

  const seekFromEvent = (e: React.MouseEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    ctl.seek(((e.clientX - r.left) / r.width) * D);
  };

  return (
    <div className="select-none" aria-label="Thread timeline. Use j and k to move between threads." role="group">
      <div className="flex">
        <div style={{ width: LABEL_W }} className="shrink-0">
          {PRIORITIES.map((p) => (
            <div
              key={p}
              style={{ height: LANE_H }}
              className={cx("flex items-center gap-1 text-[11px] pr-2 whitespace-nowrap", PRIORITY_META[p].fg)}
            >
              <span aria-hidden className={cx("size-2", PRIORITY_META[p].dot)} />
              {PRIORITY_META[p].label}
            </div>
          ))}
        </div>
        <div className="relative flex-1 min-w-0 cursor-pointer" onClick={seekFromEvent}>
          {PRIORITIES.map((p, i) => (
            <div
              key={p}
              className={cx("relative border-border-subtle", i ? "border-t" : "", i % 2 ? "bg-muted" : "bg-card")}
              style={{ height: LANE_H }}
            >
              {byLane[p].map((t) => {
                const on = visible.has(t.id);
                const sel = t.id === selectedId;
                return (
                  <button
                    key={t.id}
                    type="button"
                    tabIndex={-1}
                    title={`${t.id} ${fmtTime(t.t0)}–${fmtTime(t.t1)} · ${PRIORITY_META[t.priority].label}: ${t.title}`}
                    aria-label={`${t.id}, ${PRIORITY_META[t.priority].label}, ${fmtTime(t.t0)}: ${t.title}`}
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelect(t.id);
                    }}
                    className={cx(
                      "absolute top-[3px] bottom-[3px] min-w-[3px]",
                      PRIORITY_META[t.priority].dot,
                      !on && "opacity-20",
                      sel && "ring-2 ring-interactive ring-offset-1 ring-offset-card z-10",
                    )}
                    style={{
                      left: `${(100 * t.t0) / D}%`,
                      width: `${(100 * Math.max(0, t.t1 - t.t0)) / D}%`,
                    }}
                  />
                );
              })}
            </div>
          ))}
          <Playhead ctl={ctl} duration={D} />
        </div>
      </div>
      <div className="flex">
        <div style={{ width: LABEL_W }} className="shrink-0" />
        <div className="relative flex-1 h-4 text-[10px] text-muted-foreground tabular-nums">
          {ticks.map((s) => (
            <span key={s} className="absolute top-0 -translate-x-1/2 first:translate-x-0" style={{ left: `${(100 * s) / D}%` }}>
              {fmtTime(s, 0)}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}
