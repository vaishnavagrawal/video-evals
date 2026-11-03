"use client";
import { memo, useId, useState } from "react";
import { PRIORITIES, type Area, type FeedbackVerdict, type Issue, type Thread } from "@/lib/api/types";
import { Markdown } from "../markdown";
import { LlmMark, PRIORITY_META, PriorityBadge, PriorityIcon, btn, cx, fmtTime } from "../ui";

export interface IssueFeedback {
  verdict?: "accept" | "false_positive";
  comments: string[];
  pending: number;
}
export type FeedbackMap = Record<string, IssueFeedback | undefined>;
export type SendFeedback = (issueIds: string[], verdict: FeedbackVerdict, comment?: string) => Promise<string | null>;

export const LLM_TOOLTIP =
  "Found by the LLM judge. These findings are less repeatable: only 18–25% recurred across identical runs. Check them before acting.";
export const ON_CAMERA_TOOLTIP = "A face is on screen and likely speaking, so lip-sync errors are visible.";

export function threadTags(t: Thread, issuesById: Map<string, Issue>): string[] {
  const s = new Set<string>();
  for (const id of t.issue_ids) for (const tag of issuesById.get(id)?.tags ?? []) if (!tag.startsWith("area:")) s.add(tag);
  return [...s];
}

function TagChip({ tag }: { tag: string }) {
  const title = tag === "llm" ? LLM_TOOLTIP : tag === "on-camera" ? ON_CAMERA_TOOLTIP : undefined;
  return (
    <span
      title={title}
      className={cx(
        "inline-flex items-center rounded px-1.5 h-[18px] text-[11px] border",
        tag === "llm" ? "border-human/30 text-human bg-human-soft" : "border-line text-fg-muted bg-subtle",
      )}
    >
      {tag === "llm" ? "LLM finding" : tag}
    </span>
  );
}

function FeedbackSummary({ ids, fb }: { ids: string[]; fb: FeedbackMap }) {
  const items = ids.map((id) => fb[id]);
  const pending = items.some((f) => f && f.pending > 0);
  const acc = items.filter((f) => f?.verdict === "accept").length;
  const fp = items.filter((f) => f?.verdict === "false_positive").length;
  const comments = items.reduce((a, f) => a + (f?.comments.length ?? 0), 0);
  if (!acc && !fp && !comments && !pending) return null;
  let label = "";
  if (acc === ids.length) label = "Accepted";
  else if (fp === ids.length) label = "False positive";
  else if (acc || fp) label = `Reviewed ${acc + fp}/${ids.length}`;
  return (
    <span className="inline-flex items-center gap-1.5 text-[11px] text-fg-muted">
      {label && (
        <span
          className={cx(
            "inline-flex items-center rounded-full px-1.5 h-[18px] border",
            label === "Accepted" ? "border-ok/30 text-ok bg-ok-soft" : "border-line bg-subtle",
          )}
        >
          {label === "Accepted" ? "✓ " : label === "False positive" ? "✕ " : ""}
          {label}
        </span>
      )}
      {comments > 0 && <span>{comments === 1 ? "1 comment" : `${comments} comments`}</span>}
      {pending && <span aria-live="polite">Saving…</span>}
    </span>
  );
}

function IssueRow({ issue, fb, send }: { issue: Issue; fb?: IssueFeedback; send: SendFeedback }) {
  const conf = Math.round(issue.confidence * 100);
  const v = issue.details?.visual;
  return (
    <li className="px-3 py-2 text-[12px]">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {issue.priority && <PriorityBadge p={issue.priority} />}
        <span className="font-mono font-medium">{issue.category}</span>
        <span className="text-fg-muted">
          {issue.severity} · conf {conf}% · {fmtTime(issue.t0)}–{fmtTime(issue.t1)} · {issue.details?.source ?? issue.evaluator}
        </span>
        {(issue.tags ?? []).filter((t) => !t.startsWith("area:")).map((t) => (
          <TagChip key={t} tag={t} />
        ))}
        <span className="ml-auto inline-flex gap-1">
          <button
            type="button"
            aria-pressed={fb?.verdict === "accept"}
            onClick={() => send([issue.id], "accept")}
            className={cx(btn.base, btn.small, "h-6", fb?.verdict === "accept" ? "border-ok/40 bg-ok-soft text-ok" : btn.default)}
          >
            Accept
          </button>
          <button
            type="button"
            aria-pressed={fb?.verdict === "false_positive"}
            onClick={() => send([issue.id], "false_positive")}
            className={cx(btn.base, btn.small, "h-6", fb?.verdict === "false_positive" ? "border-fg/30 bg-muted" : btn.default)}
          >
            False positive
          </button>
        </span>
      </div>
      <p className="mt-1">{issue.description}</p>
      <p className="text-fg-muted">
        <span className="font-medium text-fg">Fix:</span> {issue.fix_hint}
      </p>
      {v && (
        <p className="text-fg-muted mt-0.5">
          Visual: {v.speaking_likely == null ? "no face found" : v.speaking_likely ? "face likely speaking" : "face not speaking"}
          {v.mouth_activity != null && ` · mouth activity ${v.mouth_activity}`}
        </p>
      )}
      <p className="font-mono text-[11px] text-fg-muted/80 mt-0.5">{issue.id}</p>
    </li>
  );
}

function ThreadBody({
  thread,
  issues,
  fb,
  send,
  srcLang,
  tgtLang,
}: {
  thread: Thread;
  issues: Issue[];
  fb: FeedbackMap;
  send: SendFeedback;
  srcLang?: string | null;
  tgtLang?: string | null;
}) {
  const [comment, setComment] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const cid = useId();
  const all = thread.issue_ids;
  const allAcc = all.every((id) => fb[id]?.verdict === "accept");
  const allFp = all.every((id) => fb[id]?.verdict === "false_positive");
  const run = async (v: FeedbackVerdict, c?: string) => {
    setErr(null);
    const e = await send(all, v, c);
    if (e) setErr(e);
    else if (v === "comment") setComment("");
  };
  const comments = all.flatMap((id) => fb[id]?.comments ?? []);
  const uniqueComments = [...new Set(comments)];

  return (
    <div className="border-t border-line-soft">
      <div className="px-3 py-2.5">
        <div className="flex items-center gap-2 mb-1 text-[12px] text-fg-muted">
          <span className="font-medium text-fg">Comment</span>
          {thread.comment_source === "llm" ? <LlmMark model={thread.comment_model} /> : <span>from rules</span>}
        </div>
        <div className="text-[13px]">
          <Markdown>{thread.comment}</Markdown>
        </div>
      </div>

      {(thread.original_text || thread.dubbed_text) && (
        <div className="grid grid-cols-2 gap-px bg-line-soft border-t border-line-soft text-[13px]">
          <div className="bg-canvas px-3 py-2">
            <div className="text-[11px] font-medium text-fg-muted mb-0.5">Original{srcLang ? ` (${srcLang})` : ""}</div>
            <p dir="auto" lang={srcLang ?? undefined}>
              {thread.original_text || <span className="text-fg-muted">–</span>}
            </p>
          </div>
          <div className="bg-canvas px-3 py-2">
            <div className="text-[11px] font-medium text-fg-muted mb-0.5">Dub{tgtLang ? ` (${tgtLang})` : ""}</div>
            <p dir="auto" lang={tgtLang ?? undefined}>
              {thread.dubbed_text || <span className="text-fg-muted">–</span>}
            </p>
          </div>
        </div>
      )}

      <details className="border-t border-line-soft group" open={issues.length <= 3}>
        <summary className="px-3 h-8 flex items-center cursor-pointer text-[12px] font-medium text-fg-muted hover:text-fg">
          {issues.length} finding{issues.length === 1 ? "" : "s"} in this thread
        </summary>
        <ul className="divide-y divide-line-soft border-t border-line-soft bg-subtle/40">
          {issues.map((i) => (
            <IssueRow key={i.id} issue={i} fb={fb[i.id]} send={send} />
          ))}
        </ul>
      </details>

      {uniqueComments.length > 0 && (
        <ul className="border-t border-line-soft px-3 py-2 space-y-1 text-[12px]">
          {uniqueComments.map((c, i) => (
            <li key={i} className="rounded border border-line bg-subtle px-2 py-1 whitespace-pre-wrap">
              <span className="text-fg-muted">You: </span>
              {c}
            </li>
          ))}
        </ul>
      )}

      <div className="border-t border-line-soft px-3 py-2.5 bg-subtle space-y-2">
        <label htmlFor={cid} className="sr-only">
          Comment on {thread.id}
        </label>
        <textarea
          id={cid}
          value={comment}
          onChange={(e) => setComment(e.target.value)}
          rows={2}
          placeholder="Leave a comment (sent with each finding in this thread)"
          className="w-full rounded-md border border-line bg-canvas px-2 py-1.5 text-[13px] resize-y"
        />
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            aria-pressed={allAcc}
            onClick={() => run("accept")}
            className={cx(btn.base, btn.small, allAcc ? "border-ok/40 bg-ok-soft text-ok" : btn.default)}
          >
            ✓ Accept{all.length > 1 ? ` all ${all.length}` : ""}
          </button>
          <button
            type="button"
            aria-pressed={allFp}
            onClick={() => run("false_positive")}
            className={cx(btn.base, btn.small, allFp ? "border-fg/30 bg-muted" : btn.default)}
          >
            ✕ False positive
          </button>
          <button
            type="button"
            disabled={!comment.trim()}
            onClick={() => run("comment", comment.trim())}
            className={cx(btn.base, btn.small, btn.default, "ml-auto")}
          >
            Comment
          </button>
        </div>
        {err && (
          <p role="alert" className="text-[12px] text-blocker">
            {err}
          </p>
        )}
      </div>
    </div>
  );
}

export const ThreadItem = memo(function ThreadItem({
  thread,
  issues,
  tags,
  expanded,
  selected,
  playing,
  fb,
  send,
  onToggle,
  onSeek,
  srcLang,
  tgtLang,
}: {
  thread: Thread;
  issues: Issue[];
  tags: string[];
  expanded: boolean;
  selected: boolean;
  playing: boolean;
  fb: FeedbackMap;
  send: SendFeedback;
  onToggle: (id: string) => void;
  onSeek: (id: string) => void;
  srcLang?: string | null;
  tgtLang?: string | null;
}) {
  const bodyId = `thread-body-${thread.id}`;
  return (
    <li
      id={`thread-${thread.id}`}
      className={cx(
        "rounded-md border bg-canvas scroll-mt-16",
        selected ? "border-accent ring-1 ring-accent" : "border-line",
      )}
    >
      <div className={cx("flex items-start gap-2 px-3 py-2", playing && "bg-[#fff8c5]")}>
        <span className={cx("mt-[3px]", PRIORITY_META[thread.priority].fg)} title={PRIORITY_META[thread.priority].label}>
          <PriorityIcon p={thread.priority} />
          <span className="sr-only">{PRIORITY_META[thread.priority].label}</span>
        </span>
        <div className="flex-1 min-w-0">
          <button
            type="button"
            data-thread-button={thread.id}
            aria-expanded={expanded}
            aria-controls={bodyId}
            onClick={() => onToggle(thread.id)}
            className="text-left font-medium text-[13px] leading-snug hover:text-accent w-full"
          >
            {thread.title}
          </button>
          <div className="flex flex-wrap items-center gap-1.5 mt-1">
            <span className="font-mono text-[11px] text-fg-muted">{thread.id}</span>
            {thread.areas.map((a) => (
              <span key={a} className="text-[11px] text-fg-muted">
                · {a}
              </span>
            ))}
            {tags.map((t) => (
              <TagChip key={t} tag={t} />
            ))}
            {thread.comment_source === "llm" && <LlmMark model={thread.comment_model} />}
            <FeedbackSummary ids={thread.issue_ids} fb={fb} />
          </div>
        </div>
        <button
          type="button"
          onClick={() => onSeek(thread.id)}
          className="shrink-0 font-mono text-[12px] tabular-nums text-accent hover:underline px-1 rounded"
          aria-label={`Seek to ${fmtTime(thread.t0)}`}
        >
          {fmtTime(thread.t0)}
        </button>
      </div>
      {expanded && (
        <div id={bodyId}>
          <ThreadBody thread={thread} issues={issues} fb={fb} send={send} srcLang={srcLang} tgtLang={tgtLang} />
        </div>
      )}
    </li>
  );
});

export interface Filters {
  areas: Set<Area>;
  tags: Set<string>;
}

export function FilterBar({
  areaCounts,
  tagCounts,
  filters,
  setFilters,
  shown,
  total,
}: {
  areaCounts: [Area, number][];
  tagCounts: [string, number][];
  filters: Filters;
  setFilters: (f: Filters) => void;
  shown: number;
  total: number;
}) {
  const toggle = <T,>(s: Set<T>, v: T) => {
    const n = new Set(s);
    if (n.has(v)) n.delete(v);
    else n.add(v);
    return n;
  };
  const chip = (on: boolean) =>
    cx(
      "inline-flex items-center gap-1 rounded-full border px-2 h-6 text-[12px]",
      on ? "border-accent bg-accent-soft text-accent font-medium" : "border-line bg-canvas text-fg-muted hover:text-fg hover:bg-subtle",
    );
  const any = filters.areas.size > 0 || filters.tags.size > 0;
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by area">
        <span className="text-[12px] text-fg-muted w-10">Area</span>
        {areaCounts.map(([a, n]) => (
          <button key={a} type="button" aria-pressed={filters.areas.has(a)} className={chip(filters.areas.has(a))} onClick={() => setFilters({ ...filters, areas: toggle(filters.areas, a) })}>
            {a} <span className="tabular-nums opacity-70">{n}</span>
          </button>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label="Filter by tag">
        <span className="text-[12px] text-fg-muted w-10">Tag</span>
        {tagCounts.map(([t, n]) => (
          <button
            key={t}
            type="button"
            aria-pressed={filters.tags.has(t)}
            title={t === "llm" ? LLM_TOOLTIP : t === "on-camera" ? ON_CAMERA_TOOLTIP : undefined}
            className={chip(filters.tags.has(t))}
            onClick={() => setFilters({ ...filters, tags: toggle(filters.tags, t) })}
          >
            {t === "llm" ? "LLM finding" : t} <span className="tabular-nums opacity-70">{n}</span>
            {t === "llm" && <span className="sr-only">. {LLM_TOOLTIP}</span>}
            {t === "llm" && (
              <span aria-hidden className="ml-0.5 inline-flex size-3.5 items-center justify-center rounded-full border border-current text-[9px]">
                ?
              </span>
            )}
          </button>
        ))}
        <span className="ml-auto text-[12px] text-fg-muted" aria-live="polite">
          {shown === total ? `${total} threads` : `${shown} of ${total} threads`}
        </span>
        {any && (
          <button type="button" className="text-[12px] text-accent hover:underline" onClick={() => setFilters({ areas: new Set(), tags: new Set() })}>
            Clear
          </button>
        )}
      </div>
      {filters.tags.has("llm") && <p className="text-[12px] text-human">{LLM_TOOLTIP}</p>}
    </div>
  );
}

export { PRIORITIES };
