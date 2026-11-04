"use client";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import {
  CaretRightIcon,
  CheckCircleIcon,
  CircleDashedIcon,
  CircleNotchIcon,
  MinusCircleIcon,
  XCircleIcon,
} from "@phosphor-icons/react";
import { api, type Pipeline, type PipelineNode, type PipelineNodeStatus } from "@/lib/api";
import { usePoll } from "@/lib/use-poll";
import { Card, CardContent } from "@/components/ui/card";
import { Spinner, cx } from "./common";

/*
 * Live view of GET /v1/reviews/{run_id}/pipeline. Fully generic: it draws whatever nodes the server returns
 * (stages, and children nested under them through `parent`) as a gantt on one shared time axis, with the
 * node's `detail` as chips. There is no list of stages in here.
 */

const POLL_MS = 2000;
/** Extra polls once the run is over but a node still says "running" (the run row flips just before the last node). */
const SETTLE_POLLS = 15;
const INDENT_PX = 18;

type Loaded = Pipeline & { fetchedAt: number };

const inFlight = (p: Pipeline) => p.status === "queued" || p.status === "running";

/** Polls the pipeline every 2 s while the run is queued or running, then stops. */
export function usePipeline(runId: string) {
  const settled = useRef(0);
  const load = useCallback(async (): Promise<Loaded> => ({ ...(await api.getPipeline(runId)), fetchedAt: Date.now() }), [runId]);
  const shouldPoll = useCallback((p: Loaded) => {
    if (inFlight(p)) {
      settled.current = 0;
      return true;
    }
    return p.nodes.some((n) => n.status === "running") && settled.current++ < SETTLE_POLLS;
  }, []);
  return usePoll(load, shouldPoll, POLL_MS);
}

/** The server's "now" in ms: ticks 4x a second while the run is in flight, corrected for browser clock skew. */
function useServerNow(data: Loaded | null): number {
  const live = !!data && inFlight(data);
  const [tick, setTick] = useState(() => Date.now());
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => setTick(Date.now()), 250);
    return () => clearInterval(timer);
  }, [live]);
  if (!data) return 0;
  const server = data.server_time ? Date.parse(data.server_time) : NaN;
  if (!Number.isFinite(server)) return tick;
  return live ? Math.max(tick + (server - data.fetchedAt), server) : server;
}

// --- formatting ---------------------------------------------------------------------------------------------------

export function fmtDuration(seconds: number): string {
  const s = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  if (s < 10) return `${s.toFixed(1)}s`;
  const total = Math.round(s);
  if (total < 60) return `${total}s`;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (total < 3600) return `${Math.floor(total / 60)}m ${pad(total % 60)}s`;
  return `${Math.floor(total / 3600)}h ${pad(Math.floor((total % 3600) / 60))}m`;
}

const fmtAxis = (seconds: number) => (seconds < 1 ? "0s" : fmtDuration(seconds).replace(/\.\d/, ""));

const fmtClock = (ms: number) => new Date(ms).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

const fmtNum = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1));

const prettyKey = (key: string) => key.replace(/_/g, " ");

const truncate = (text: string, max: number) => (text.length > max ? `${text.slice(0, max - 1)}…` : text);

type Tone = "default" | "good" | "warn";

interface Chip {
  label?: string;
  value: string;
  tone?: Tone;
  mono?: boolean;
  title?: string;
}

/** Detail keys shown as text lines under the row instead of chips. */
const LINE_KEYS = new Set(["error", "reason"]);
/** Chips for these come first, in this order. */
const FIRST_KEYS = ["model", "models", "cached", "score", "issues"];

const VERDICT_TEXT: Record<string, { text: string; tone: Tone }> = {
  request_changes: { text: "changes requested", tone: "warn" },
  needs_human: { text: "needs human", tone: "warn" },
  approve: { text: "approved", tone: "good" },
};

/** One node's `detail` as chips. Known keys get friendly text; any other key shows as `key: value`. */
export function detailChips(detail: Record<string, unknown> | null | undefined): Chip[] {
  const rank = (k: string) => {
    const i = FIRST_KEYS.indexOf(k);
    return i < 0 ? FIRST_KEYS.length : i;
  };
  const keys = Object.keys(detail ?? {})
    .filter((k) => !LINE_KEYS.has(k))
    .sort((a, b) => rank(a) - rank(b));

  const chips: Chip[] = [];
  for (const key of keys) {
    const v = detail![key];
    if (v == null || v === "") continue;
    const label = prettyKey(key);

    if (key === "model" || key === "models") {
      for (const m of Array.isArray(v) ? v : [v]) chips.push({ label: "model", value: String(m), mono: true });
    } else if (typeof v === "boolean") {
      if (key === "cached") {
        if (v) chips.push({ value: "cached", tone: "good" });
      } else chips.push(v ? { value: label } : { label, value: "no" });
    } else if (key === "issues" && typeof v === "number") {
      chips.push({ value: `${fmtNum(v)} ${v === 1 ? "issue" : "issues"}` });
    } else if (key === "score" && typeof v === "number") {
      chips.push({ label: "score", value: fmtNum(v) });
    } else if (key === "attempt" && typeof v === "number") {
      if (v > 1) chips.push({ value: `attempt ${v}`, tone: "warn" });
    } else if (key === "warnings") {
      const n = Array.isArray(v) ? v.length : typeof v === "number" ? v : 0;
      if (n > 0) chips.push({ value: `${n} ${n === 1 ? "warning" : "warnings"}`, tone: "warn" });
    } else if (key === "verdict" && typeof v === "string") {
      const meta = VERDICT_TEXT[v];
      chips.push({ label: "verdict", value: meta?.text ?? prettyKey(v), tone: meta?.tone });
    } else if (typeof v === "number") {
      chips.push({ label, value: fmtNum(v) });
    } else if (typeof v === "string") {
      chips.push({ label, value: truncate(v, 48), title: v.length > 48 ? v : undefined });
    } else if (Array.isArray(v)) {
      if (!v.length) continue;
      const scalar = v.every((x) => x == null || typeof x !== "object");
      const text = scalar ? v.join(", ") : `${v.length} items`;
      chips.push({ label, value: truncate(text, 48), title: text.length > 48 ? text : undefined });
    } else {
      const text = JSON.stringify(v);
      chips.push({ label, value: truncate(text, 48), title: text.length > 48 ? text : undefined, mono: true });
    }
  }
  return chips;
}

const TONE_CLASS: Record<Tone, string> = {
  default: "border-border bg-muted/40 text-foreground",
  good: "border-success/30 bg-success-soft text-success",
  warn: "border-shouldfix/30 bg-shouldfix-soft text-shouldfix",
};

function ChipView({ chip }: { chip: Chip }) {
  return (
    <span
      title={chip.title}
      className={cx("inline-flex items-center gap-1 h-5 px-1.5 border text-[11px] leading-none", TONE_CLASS[chip.tone ?? "default"])}
    >
      {chip.label && <span className={chip.tone ? "opacity-80" : "text-muted-foreground"}>{chip.label}</span>}
      <span className={cx(chip.mono && "font-mono", "tabular-nums")}>{chip.value}</span>
    </span>
  );
}

const STATUS_TEXT: Record<string, string> = {
  pending: "Pending",
  running: "Running",
  succeeded: "Done",
  failed: "Failed",
  skipped: "Skipped",
};

function StatusIcon({ status }: { status: PipelineNodeStatus }) {
  const cls = "size-4 shrink-0";
  return (
    <span className="inline-flex" title={STATUS_TEXT[status] ?? status}>
      {status === "succeeded" ? (
        <CheckCircleIcon aria-hidden weight="fill" className={cx(cls, "text-success")} />
      ) : status === "failed" ? (
        <XCircleIcon aria-hidden weight="fill" className={cx(cls, "text-blocker")} />
      ) : status === "running" ? (
        <CircleNotchIcon aria-hidden weight="bold" className={cx(cls, "text-link animate-spin motion-reduce:animate-none")} />
      ) : status === "skipped" ? (
        <MinusCircleIcon aria-hidden className={cx(cls, "text-muted-foreground")} />
      ) : (
        <CircleDashedIcon aria-hidden className={cx(cls, "text-muted-foreground")} />
      )}
      <span className="sr-only">{STATUS_TEXT[status] ?? status}</span>
    </span>
  );
}

// --- layout -------------------------------------------------------------------------------------------------------

interface Row {
  node: PipelineNode;
  depth: number;
  start: number;
  end: number;
}

interface Layout {
  rows: Row[];
  t0: number;
  span: number;
}

/** Parents first, each followed by its children (any depth); a node whose parent is unknown is shown at the top level. */
function buildLayout(nodes: PipelineNode[], nowMs: number): Layout {
  const ids = new Set(nodes.map((n) => n.node));
  const kids = new Map<string, PipelineNode[]>();
  const roots: PipelineNode[] = [];
  for (const n of nodes) {
    if (n.parent && n.parent !== n.node && ids.has(n.parent)) kids.set(n.parent, [...(kids.get(n.parent) ?? []), n]);
    else roots.push(n);
  }

  const times = (n: PipelineNode) => {
    const start = Date.parse(n.started_at);
    const finished = n.finished_at ? Date.parse(n.finished_at) : NaN;
    const s = Number.isFinite(start) ? start : 0;
    // A running node ends "now"; one that is not running and has no finish time is shown as a thin mark.
    const e = Number.isFinite(finished) ? finished : n.status === "running" ? nowMs : s;
    return { start: s, end: Math.max(s, e) };
  };

  const rows: Row[] = [];
  const seen = new Set<string>();
  const walk = (n: PipelineNode, depth: number) => {
    if (seen.has(n.node)) return;
    seen.add(n.node);
    rows.push({ node: n, depth, ...times(n) });
    for (const c of kids.get(n.node) ?? []) walk(c, depth + 1);
  };
  roots.forEach((r) => walk(r, 0));
  nodes.forEach((n) => walk(n, 0)); // anything in a parent cycle

  const t0 = rows.length ? Math.min(...rows.map((r) => r.start)) : 0;
  const tEnd = rows.length ? Math.max(...rows.map((r) => r.end)) : 0;
  return { rows, t0, span: Math.max(tEnd - t0, 1000) };
}

function summarize(layout: Layout): string {
  const steps = layout.rows.filter((r) => r.depth === 0).length;
  const failed = layout.rows.filter((r) => r.node.status === "failed").length;
  const running = layout.rows.some((r) => r.node.status === "running");
  const total = fmtDuration(layout.span / 1000);
  if (running) return `Running · ${total} elapsed`;
  const count = `${steps} ${steps === 1 ? "step" : "steps"}`;
  return failed ? `${count} · ${failed} failed · ${total}` : `${count} · finished in ${total}`;
}

// --- chart --------------------------------------------------------------------------------------------------------

const BAR_CLASS: Record<string, string> = {
  succeeded: "bg-interactive",
  running: "bg-interactive/60 animate-pulse motion-reduce:animate-none",
  failed: "bg-blocker",
  skipped: "border border-dashed border-border-strong",
};

const TICKS = [0, 0.25, 0.5, 0.75, 1];

function ChartRow({ row, layout }: { row: Row; layout: Layout }) {
  const { node, depth, start, end } = row;
  const left = ((start - layout.t0) / layout.span) * 100;
  const width = Math.min(((end - start) / layout.span) * 100, 100 - left);
  const seconds = node.status === "running" || node.duration_s == null ? (end - start) / 1000 : node.duration_s;
  const chips = detailChips(node.detail);
  const error = typeof node.detail?.error === "string" ? node.detail.error : null;
  const reason = typeof node.detail?.reason === "string" ? node.detail.reason : null;
  const pad = depth * INDENT_PX;
  const when = `${fmtClock(start)} → ${node.finished_at ? fmtClock(end) : "running"}`;

  return (
    <li data-node={node.node} data-status={node.status} className="py-2.5 first:pt-0 last:pb-0">
      <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1.5 sm:grid-cols-[minmax(0,260px)_minmax(0,1fr)_64px]">
        <div className="order-1 flex min-w-0 items-center gap-2" style={{ paddingLeft: pad }}>
          {depth > 0 && (
            <span aria-hidden className="-mr-0.5 text-muted-foreground">
              ↳
            </span>
          )}
          <StatusIcon status={node.status} />
          <span
            title={node.node}
            className={cx("truncate", depth === 0 ? "text-sm" : "text-[13px]", node.status === "pending" && "text-muted-foreground")}
          >
            {node.label}
          </span>
        </div>
        <span
          className={cx(
            "order-2 text-right text-xs tabular-nums sm:order-3",
            node.status === "running" ? "text-link" : "text-muted-foreground",
          )}
        >
          {node.status === "skipped" && !seconds ? "–" : fmtDuration(seconds)}
        </span>
        <div
          aria-hidden
          title={when}
          className="order-3 col-span-2 relative h-4 bg-[linear-gradient(to_right,var(--border-subtle)_1px,transparent_1px)] [background-size:25%_100%] sm:order-2 sm:col-span-1"
        >
          <span
            className={cx(
              "absolute top-1/2 -translate-y-1/2 min-w-[3px]",
              depth === 0 ? "h-3.5" : "h-2.5",
              BAR_CLASS[node.status] ?? "bg-border",
            )}
            style={{ left: `${left}%`, width: `${Math.max(width, 0)}%` }}
          />
        </div>
      </div>
      {(chips.length > 0 || error || reason) && (
        <div className="mt-1.5 space-y-1" style={{ paddingLeft: pad + (depth > 0 ? 38 : 24) }}>
          {chips.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {chips.map((c, i) => (
                <ChipView key={`${c.label ?? ""}:${c.value}:${i}`} chip={c} />
              ))}
            </div>
          )}
          {reason && <p className="text-xs text-muted-foreground break-words">{node.status === "skipped" ? `Skipped: ${reason}` : reason}</p>}
          {error && <p className="text-xs text-blocker font-mono break-words">{error}</p>}
        </div>
      )}
    </li>
  );
}

/** The gantt itself: a time axis and one row per node. */
function PipelineChart({ layout }: { layout: Layout }) {
  return (
    <div>
      <div
        aria-hidden
        className="mb-2 hidden items-end gap-x-3 border-b border-border-subtle pb-1 sm:grid sm:grid-cols-[minmax(0,260px)_minmax(0,1fr)_64px]"
      >
        <span />
        <div className="relative h-4 text-[10px] tabular-nums text-muted-foreground">
          {TICKS.map((f) => (
            <span
              key={f}
              className={cx("absolute whitespace-nowrap", f === 0 ? "" : f === 1 ? "-translate-x-full" : "-translate-x-1/2")}
              style={{ left: `${f * 100}%` }}
            >
              {fmtAxis((layout.span / 1000) * f)}
            </span>
          ))}
        </div>
        <span />
      </div>
      <ol aria-label="Pipeline steps" className="divide-y divide-border-subtle">
        {layout.rows.map((row) => (
          <ChartRow key={row.node.node} row={row} layout={layout} />
        ))}
      </ol>
    </div>
  );
}

/** The loaded pipeline laid out on its time axis (null until it has loaded). */
function useLayout(data: Loaded | null): Layout | null {
  const nowMs = useServerNow(data);
  const nodes = data?.nodes;
  return useMemo(() => (nodes ? buildLayout(nodes, nowMs) : null), [nodes, nowMs]);
}

// --- views --------------------------------------------------------------------------------------------------------

function Unavailable() {
  return <p className="text-sm text-muted-foreground">Pipeline details were not recorded for this review.</p>;
}

/**
 * Live pipeline for a run in progress (or failed). While the server has no nodes for the run (an older run, or a
 * worker that has not started yet) it shows `fallback` instead. `error` is the run-level error, shown under the chart.
 */
export function PipelineView({ runId, fallback, error: runError }: { runId: string; fallback?: ReactNode; error?: string | null }) {
  const { data, error } = usePipeline(runId);
  const layout = useLayout(data);

  if (!data) return error ? <>{fallback ?? <Unavailable />}</> : <Spinner label="Loading pipeline" />;
  if (!layout || data.nodes.length === 0) return <>{fallback ?? <Unavailable />}</>;

  return (
    <Card role="region" aria-label="Pipeline" className="gap-0 py-0">
      <div className="flex h-10 items-center gap-2 border-b px-4 text-xs">
        <h2 className="text-sm font-semibold">Pipeline</h2>
        <span className="text-muted-foreground">{summarize(layout)}</span>
      </div>
      <CardContent className="px-4 py-3">
        <PipelineChart layout={layout} />
        {runError && <p className="mt-3 border-t border-border-subtle pt-2 text-xs text-blocker font-mono break-words">{runError}</p>}
      </CardContent>
    </Card>
  );
}

/** Collapsible "Pipeline" card for a finished review: how it was produced. Renders nothing for runs without nodes. */
export function PipelineSection({ runId }: { runId: string }) {
  const { data } = usePipeline(runId);
  const layout = useLayout(data);
  const [open, setOpen] = useState(false);
  const bodyId = useId();

  if (!layout || !data || data.nodes.length === 0) return null;

  return (
    <Card role="region" aria-label="Pipeline" className="mb-4 gap-0 py-0">
      <h2 className="m-0">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => setOpen((o) => !o)}
          className="flex h-10 w-full items-center gap-2 px-4 text-left text-xs hover:bg-accent focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring"
        >
          <CaretRightIcon aria-hidden className={cx("size-3.5 shrink-0 transition-transform motion-reduce:transition-none", open && "rotate-90")} />
          <span className="text-sm font-semibold">Pipeline</span>
          <span className="text-muted-foreground">How this review was produced · {summarize(layout)}</span>
        </button>
      </h2>
      {open && (
        <div id={bodyId} className="border-t px-4 py-3">
          <PipelineChart layout={layout} />
        </div>
      )}
    </Card>
  );
}
