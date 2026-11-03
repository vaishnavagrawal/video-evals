import type { ReactNode } from "react";
import type { Priority, PriorityCounts, RunStatus, Verdict } from "@/lib/api/types";

export function cx(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(" ");
}

export function fmtTime(t: number | null | undefined, digits = 1): string {
  if (t == null || !Number.isFinite(t)) return "–";
  const m = Math.floor(t / 60);
  const s = (t - m * 60).toFixed(digits).padStart(digits ? 3 + digits : 2, "0");
  return `${m}:${s}`;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "–";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const u = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 ? 1 : 0)} ${u[i]}`;
}

export const btn = {
  base: "inline-flex items-center justify-center gap-1.5 rounded-md border px-3 h-8 text-[13px] font-medium whitespace-nowrap transition-colors disabled:opacity-50 disabled:cursor-not-allowed",
  default: "border-line bg-subtle text-fg hover:bg-muted",
  primary: "border-[#1f883d] bg-[#1f883d] text-white hover:bg-[#1a7f37]",
  danger: "border-line bg-subtle text-blocker hover:bg-blocker-soft",
  small: "h-7 px-2 text-[12px]",
};

export const PRIORITY_META: Record<Priority, { label: string; fg: string; bg: string; dot: string }> = {
  blocker: { label: "Blocker", fg: "text-blocker", bg: "bg-blocker-soft", dot: "bg-blocker" },
  "should-fix": { label: "Should fix", fg: "text-shouldfix", bg: "bg-shouldfix-soft", dot: "bg-shouldfix" },
  "needs-human": { label: "Needs human", fg: "text-human", bg: "bg-human-soft", dot: "bg-human" },
  nit: { label: "Nit", fg: "text-nit", bg: "bg-nit-soft", dot: "bg-nit" },
};

export const PRIORITY_HEX: Record<Priority, string> = {
  blocker: "#cf222e",
  "should-fix": "#bc4c00",
  "needs-human": "#8250df",
  nit: "#8c959f",
};

/** Small glyphs so priority is never conveyed by color alone. */
export function PriorityIcon({ p, className }: { p: Priority; className?: string }) {
  const c = cx("inline-block shrink-0", className);
  switch (p) {
    case "blocker":
      return (
        <svg viewBox="0 0 16 16" width="12" height="12" className={c} aria-hidden fill="currentColor">
          <path d="M4.47.22A.75.75 0 0 1 5 0h6a.75.75 0 0 1 .53.22l4.25 4.25c.141.14.22.331.22.53v6a.75.75 0 0 1-.22.53l-4.25 4.25A.75.75 0 0 1 11 16H5a.75.75 0 0 1-.53-.22L.22 11.53A.75.75 0 0 1 0 11V5a.75.75 0 0 1 .22-.53Zm.84 1.28L1.5 5.31v5.38l3.81 3.81h5.38l3.81-3.81V5.31L10.69 1.5ZM8 4a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 8 4Zm0 8a1 1 0 1 1 0-2 1 1 0 0 1 0 2Z" />
        </svg>
      );
    case "should-fix":
      return (
        <svg viewBox="0 0 16 16" width="12" height="12" className={c} aria-hidden fill="currentColor">
          <path d="M6.457 1.047c.659-1.234 2.427-1.234 3.086 0l6.082 11.378A1.75 1.75 0 0 1 14.082 15H1.918a1.75 1.75 0 0 1-1.543-2.575Zm1.763.707a.25.25 0 0 0-.44 0L1.698 13.132a.25.25 0 0 0 .22.368h12.164a.25.25 0 0 0 .22-.368Zm.53 3.996v2.5a.75.75 0 0 1-1.5 0v-2.5a.75.75 0 0 1 1.5 0ZM9 11a1 1 0 1 1-2 0 1 1 0 0 1 2 0Z" />
        </svg>
      );
    case "needs-human":
      return (
        <svg viewBox="0 0 16 16" width="12" height="12" className={c} aria-hidden fill="currentColor">
          <path d="M0 8a8 8 0 1 1 16 0A8 8 0 0 1 0 8Zm8-6.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM6.92 6.085h.001a.749.749 0 1 1-1.342-.67c.169-.339.436-.701.849-.977C6.845 4.16 7.369 4 8 4a2.756 2.756 0 0 1 1.637.525c.503.377.863.965.863 1.725 0 .448-.115.83-.329 1.15-.205.307-.47.513-.692.662-.109.072-.22.138-.313.195l-.006.004a6.24 6.24 0 0 0-.26.16.952.952 0 0 0-.276.245.75.75 0 0 1-1.248-.832c.184-.264.42-.489.692-.661.103-.067.207-.132.313-.195l.007-.004c.1-.061.182-.11.258-.161a.969.969 0 0 0 .277-.245C8.96 6.514 9 6.427 9 6.25a.612.612 0 0 0-.262-.525A1.27 1.27 0 0 0 8 5.5c-.369 0-.595.09-.74.187a1.01 1.01 0 0 0-.34.398ZM9 11a1 1 0 1 1-2 0 1 1 0 0 1 2 0Z" />
        </svg>
      );
    default:
      return (
        <svg viewBox="0 0 16 16" width="12" height="12" className={c} aria-hidden fill="currentColor">
          <path d="M8 4a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z" />
        </svg>
      );
  }
}

export function PriorityBadge({ p, count, compact }: { p: Priority; count?: number; compact?: boolean }) {
  const m = PRIORITY_META[p];
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 rounded-full border border-transparent px-2 h-5 text-[12px] font-medium",
        m.fg,
        m.bg,
      )}
    >
      <PriorityIcon p={p} />
      {!compact && <span>{m.label}</span>}
      {count != null && <span className="tabular-nums">{count}</span>}
      {compact && <span className="sr-only">{m.label}</span>}
    </span>
  );
}

/** Dense counts cell: icon + number per priority, labelled for screen readers and on hover. */
export function PriorityCountsInline({ counts }: { counts: PriorityCounts | null | undefined }) {
  if (!counts) return <span className="text-fg-muted">–</span>;
  const order: Priority[] = ["blocker", "should-fix", "needs-human", "nit"];
  return (
    <span className="inline-flex items-center gap-2 whitespace-nowrap">
      {order.map((p) => (
        <span
          key={p}
          title={`${counts[p]} ${PRIORITY_META[p].label.toLowerCase()} threads`}
          className={cx(
            "inline-flex items-center gap-1 tabular-nums text-[12px]",
            counts[p] ? PRIORITY_META[p].fg : "text-fg-muted/60",
          )}
        >
          <PriorityIcon p={p} />
          <span>{counts[p]}</span>
          <span className="sr-only">{PRIORITY_META[p].label}</span>
        </span>
      ))}
    </span>
  );
}

const VERDICT_META: Record<Verdict, { label: string; cls: string; icon: ReactNode }> = {
  request_changes: {
    label: "Changes requested",
    cls: "text-blocker border-blocker/30 bg-blocker-soft",
    icon: <PriorityIcon p="blocker" />,
  },
  needs_human: {
    label: "Needs human",
    cls: "text-human border-human/30 bg-human-soft",
    icon: <PriorityIcon p="needs-human" />,
  },
  approve: {
    label: "Approved",
    cls: "text-ok border-ok/30 bg-ok-soft",
    icon: (
      <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden fill="currentColor">
        <path d="M13.78 4.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 9.28a.751.751 0 0 1 .018-1.042.751.751 0 0 1 1.042-.018L6 10.94l6.72-6.72a.75.75 0 0 1 1.06 0Z" />
      </svg>
    ),
  },
};

export function VerdictBadge({ verdict, large }: { verdict: Verdict | null | undefined; large?: boolean }) {
  if (!verdict) return <span className="text-fg-muted">–</span>;
  const m = VERDICT_META[verdict];
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1.5 rounded-full border font-medium whitespace-nowrap",
        large ? "px-3 h-7 text-[13px]" : "px-2 h-5 text-[12px]",
        m.cls,
      )}
    >
      {m.icon}
      {m.label}
    </span>
  );
}

export function StatusBadge({ status }: { status: RunStatus }) {
  const meta: Record<RunStatus, { label: string; cls: string }> = {
    queued: { label: "Queued", cls: "text-fg-muted border-line bg-subtle" },
    running: { label: "Running", cls: "text-accent border-accent/30 bg-accent-soft" },
    succeeded: { label: "Done", cls: "text-fg border-line bg-canvas" },
    failed: { label: "Failed", cls: "text-blocker border-blocker/30 bg-blocker-soft" },
  };
  const m = meta[status] ?? meta.queued;
  return (
    <span className={cx("inline-flex items-center gap-1.5 rounded-full border px-2 h-5 text-[12px] font-medium", m.cls)}>
      {status === "running" && <span className="size-1.5 rounded-full bg-accent animate-pulse" aria-hidden />}
      {m.label}
    </span>
  );
}

export function LlmMark({ model }: { model?: string | null }) {
  const label = model ? `LLM-written · ${model}` : "LLM-written";
  return (
    <span
      className="inline-flex items-center gap-1 rounded border border-line px-1.5 h-[18px] text-[11px] font-medium text-fg-muted bg-subtle"
      title={`Written by ${model ? `the LLM ${model}` : "an LLM"} from the findings. Wording can vary between runs; check the member findings.`}
    >
      <svg viewBox="0 0 16 16" width="10" height="10" aria-hidden fill="currentColor">
        <path d="M7.53 1.282a.5.5 0 0 1 .94 0l.478 1.306a7.492 7.492 0 0 0 4.464 4.464l1.305.478a.5.5 0 0 1 0 .94l-1.305.478a7.492 7.492 0 0 0-4.464 4.464l-.478 1.305a.5.5 0 0 1-.94 0l-.478-1.305a7.492 7.492 0 0 0-4.464-4.464L1.282 8.47a.5.5 0 0 1 0-.94l1.306-.478a7.492 7.492 0 0 0 4.464-4.464Z" />
      </svg>
      {label}
    </span>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-fg-muted">
      <span className="size-3.5 rounded-full border-2 border-line border-t-accent animate-spin" aria-hidden />
      {label}
    </span>
  );
}

export function ErrorBox({ children }: { children: ReactNode }) {
  return (
    <div role="alert" className="rounded-md border border-blocker/30 bg-blocker-soft px-3 py-2 text-[13px] text-blocker">
      {children}
    </div>
  );
}
