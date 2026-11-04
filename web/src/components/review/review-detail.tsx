"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import {
  PRIORITIES,
  PRIORITY_RANK,
  type Area,
  type FeedbackVerdict,
  type Issue,
  type MediaUrl,
  type Review,
  type Thread,
  type Triage,
} from "@/lib/api/types";
import { areaOf, priorityOf, tagsOf, triage as computeTriage } from "@/lib/triage";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Kbd } from "@/components/ui/kbd";
import { Markdown } from "../markdown";
import { LlmMark, PRIORITY_META, PriorityBadge, VerdictBadge, cx, fmtDate } from "../common";
import { Players } from "./players";
import { SyncController, useSync } from "./sync";
import { Timeline } from "./timeline";
import { FilterBar, ThreadItem, threadTags, type FeedbackMap, type Filters } from "./thread-list";

function useTriage(review: Review): { triage: Triage; issues: Issue[]; computedLocally: boolean } {
  return useMemo(() => {
    if (review.triage) {
      const issues = structuredClone(review.results.flatMap((r) => r.issues));
      // Old payloads may lack per-issue fields; derive them with the same deterministic rules.
      for (const i of issues) {
        i.area ??= areaOf(i.category);
        i.priority ??= priorityOf(i);
        i.tags ??= tagsOf(i);
      }
      return { triage: review.triage, issues, computedLocally: false };
    }
    const clone = structuredClone(review);
    const t = computeTriage(clone);
    return { triage: t, issues: clone.results.flatMap((r) => r.issues), computedLocally: true };
  }, [review]);
}

function isTyping(el: EventTarget | null) {
  const e = el as HTMLElement | null;
  if (!e) return false;
  return e.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(e.tagName);
}

export function ReviewDetail({ review }: { review: Review }) {
  const { triage, issues, computedLocally } = useTriage(review);
  const issuesById = useMemo(() => new Map(issues.map((i) => [i.id, i])), [issues]);
  const threads = triage.threads;
  const [ctl] = useState(() => new SyncController());
  useEffect(() => () => ctl.destroy(), [ctl]);

  const fallbackDuration = useMemo(() => {
    let d = 0;
    for (const r of review.results)
      for (const v of Object.values(r.summary ?? {}))
        if (v && typeof v === "object" && typeof (v as { duration?: unknown }).duration === "number")
          d = Math.max(d, (v as { duration: number }).duration);
    for (const t of threads) d = Math.max(d, t.t1);
    return d;
  }, [review, threads]);
  useEffect(() => ctl.setFallbackDuration(fallbackDuration), [ctl, fallbackDuration]);
  const duration = useSync(ctl, (s) => s.duration || fallbackDuration);

  // Media URLs (presigned GETs).
  const mediaRefs = review.media;
  const origRef = mediaRefs.find((m) => m.role === "original");
  const dubRef = mediaRefs.find((m) => m.role === "candidate");
  const refImages = mediaRefs.filter((m) => m.role === "reference");
  const [media, setMedia] = useState<Record<string, MediaUrl | null>>({});
  const mediaKey = mediaRefs.map((m) => m.asset_id).join(",");
  useEffect(() => {
    let off = false;
    for (const assetId of mediaKey ? mediaKey.split(",") : [])
      api
        .getMedia(review.run_id, assetId)
        .then((u) => !off && setMedia((s) => ({ ...s, [assetId]: u })))
        .catch(() => !off && setMedia((s) => ({ ...s, [assetId]: null })));
    return () => {
      off = true;
    };
  }, [mediaKey, review.run_id]);
  const mediaOf = (ref?: { asset_id: string }) => (ref ? media[ref.asset_id] : null);

  // Filters.
  const [filters, setFilters] = useState<Filters>({ areas: new Set(), tags: new Set() });
  const tagsByThread = useMemo(() => new Map(threads.map((t) => [t.id, threadTags(t, issuesById)])), [threads, issuesById]);
  const areaCounts = useMemo(() => {
    const m = new Map<Area, number>();
    for (const t of threads) for (const a of t.areas) m.set(a, (m.get(a) ?? 0) + 1);
    return [...m.entries()].sort((a, b) => b[1] - a[1]);
  }, [threads]);
  const tagCounts = useMemo(() => {
    const m = new Map<string, number>([
      ["llm", 0],
      ["on-camera", 0],
    ]);
    for (const tags of tagsByThread.values()) for (const t of tags) m.set(t, (m.get(t) ?? 0) + 1);
    return [...m.entries()];
  }, [tagsByThread]);

  const visible = useMemo(() => {
    const ok = (t: Thread) =>
      (!filters.areas.size || t.areas.some((a) => filters.areas.has(a))) &&
      (!filters.tags.size || (tagsByThread.get(t.id) ?? []).some((x) => filters.tags.has(x)));
    return threads
      .filter(ok)
      .sort((a, b) => PRIORITY_RANK[b.priority] - PRIORITY_RANK[a.priority] || a.t0 - b.t0 || a.id.localeCompare(b.id));
  }, [threads, filters, tagsByThread]);
  const visibleIds = useMemo(() => new Set(visible.map((t) => t.id)), [visible]);
  const groups = useMemo(() => PRIORITIES.map((p) => [p, visible.filter((t) => t.priority === p)] as const), [visible]);

  // Selection / expansion / keyboard.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const threadById = useMemo(() => new Map(threads.map((t) => [t.id, t])), [threads]);

  const select = useCallback(
    (id: string, opts: { seek?: boolean; focus?: boolean; expand?: boolean } = {}) => {
      setSelectedId(id);
      const t = threadById.get(id);
      if (t && opts.seek !== false) ctl.seek(Math.max(0, t.t0 - 0.5));
      if (opts.expand) setExpanded((s) => new Set(s).add(id));
      requestAnimationFrame(() => {
        document.getElementById(`thread-${id}`)?.scrollIntoView({ block: "nearest" });
        if (opts.focus) (document.querySelector(`[data-thread-button="${id}"]`) as HTMLElement | null)?.focus({ preventScroll: true });
      });
    },
    [ctl, threadById],
  );
  const move = useCallback(
    (dir: 1 | -1) => {
      if (!visible.length) return;
      const i = visible.findIndex((t) => t.id === selectedId);
      const n = i < 0 ? (dir > 0 ? 0 : visible.length - 1) : Math.min(visible.length - 1, Math.max(0, i + dir));
      select(visible[n].id, { focus: true });
    },
    [visible, selectedId, select],
  );
  const toggle = useCallback(
    (id: string) => {
      setExpanded((s) => {
        const n = new Set(s);
        if (n.has(id)) n.delete(id);
        else n.add(id);
        return n;
      });
      select(id, { seek: true });
    },
    [select],
  );
  const seekTo = useCallback((id: string) => select(id, { seek: true }), [select]);

  const keyRef = useRef<(e: KeyboardEvent) => void>(() => {});
  useEffect(() => {
    keyRef.current = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return;
      const tag = (e.target as HTMLElement | null)?.tagName;
      if (e.key === "j") move(1);
      else if (e.key === "k") move(-1);
      else if (e.key === "o" && selectedId) toggle(selectedId);
      else if (e.key === "a") ctl.setAudio(ctl.get().audio === "dub" ? "original" : "dub");
      else if (e.key === " " && tag !== "BUTTON" && tag !== "A" && tag !== "SUMMARY") ctl.toggle();
      else if (e.key === "ArrowLeft") ctl.nudge(e.shiftKey ? -1 : -5);
      else if (e.key === "ArrowRight") ctl.nudge(e.shiftKey ? 1 : 5);
      else return;
      e.preventDefault();
    };
  });
  useEffect(() => {
    const h = (e: KeyboardEvent) => keyRef.current(e);
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, []);

  // Thread under the playhead (re-renders only when the id changes).
  const playingId = useSync(ctl, (s) => {
    for (const t of visible) if (s.t >= t.t0 && s.t <= t.t1) return t.id;
    return null;
  });

  // Feedback, optimistic.
  const [fb, setFb] = useState<FeedbackMap>({});
  const [fbLoadError, setFbLoadError] = useState<string | null>(null);
  useEffect(() => {
    let off = false;
    api
      .listFeedback(review.run_id)
      .then((rows) => {
        if (off) return;
        const saved: FeedbackMap = {};
        for (const r of [...rows].sort((a, b) => a.created_at.localeCompare(b.created_at))) {
          const cur = saved[r.issue_id] ?? { comments: [], pending: 0 };
          saved[r.issue_id] =
            r.verdict === "comment"
              ? { ...cur, comments: r.comment ? [...cur.comments, r.comment] : cur.comments }
              : { ...cur, verdict: r.verdict };
        }
        // Anything the user did before the load finished wins.
        setFb((s) => ({ ...saved, ...s }));
      })
      .catch((e) => !off && setFbLoadError((e as Error).message));
    return () => {
      off = true;
    };
  }, [review.run_id]);
  const fbRef = useRef(fb);
  useEffect(() => {
    fbRef.current = fb;
  }, [fb]);
  const send = useCallback(
    async (ids: string[], verdict: FeedbackVerdict, comment?: string): Promise<string | null> => {
      const prev = Object.fromEntries(ids.map((id) => [id, fbRef.current[id]]));
      setFb((s) => {
        const n = { ...s };
        for (const id of ids) {
          const cur = n[id] ?? { comments: [], pending: 0 };
          n[id] = {
            verdict: verdict === "comment" ? cur.verdict : verdict,
            comments: verdict === "comment" && comment ? [...cur.comments, comment] : cur.comments,
            pending: cur.pending + 1,
          };
        }
        return n;
      });
      const res = await Promise.allSettled(ids.map((id) => api.sendFeedback(review.run_id, id, verdict, comment)));
      const failed = ids.filter((_, i) => res[i].status === "rejected");
      setFb((s) => {
        const n = { ...s };
        for (const id of ids) {
          if (failed.includes(id)) n[id] = prev[id];
          else if (n[id]) n[id] = { ...n[id]!, pending: Math.max(0, n[id]!.pending - 1) };
        }
        return n;
      });
      if (!failed.length) return null;
      const reason = (res.find((r) => r.status === "rejected") as PromiseRejectedResult).reason as Error;
      return `Could not save feedback for ${failed.length} of ${ids.length} finding${ids.length > 1 ? "s" : ""}: ${reason.message}`;
    },
    [review.run_id],
  );

  const result = review.results[0];
  const brief = review.brief;
  const srcLang = brief.source_language;
  const tgtLang = brief.target_languages?.[0];
  const tracks = [...new Set(threads.map((t) => t.track).filter(Boolean))];
  const warnings = review.results.flatMap((r) => r.warnings.map((w) => `${r.evaluator}: ${w}`));
  const notOk = review.results.filter((r) => r.status !== "ok");

  return (
    <div className="px-4 py-5 lg:px-6">
      {/* Header */}
      <div className="mb-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <h1 className="text-2xl font-normal">{dubRef?.filename ?? review.run_id}</h1>
          <VerdictBadge verdict={triage.verdict} large />
          <span className="text-sm">
            Score <span className="font-semibold tabular-nums">{review.score == null ? "–" : review.score.toFixed(1)}</span>
            <span className="text-muted-foreground">/100</span>
          </span>
          <span className="flex flex-wrap gap-1.5" aria-label="Thread counts">
            {PRIORITIES.map((p) => (
              <PriorityBadge key={p} p={p} count={triage.counts[p] ?? 0} />
            ))}
          </span>
        </div>
        <p className="text-xs text-muted-foreground mt-2">
          <span className="font-mono">{review.run_id}</span> · {fmtDate(review.created_at)}
          {origRef?.filename && <> · vs {origRef.filename}</>}
          {tracks.length > 0 && <> · track {tracks.join(", ")}</>}
          {!tracks.length && (srcLang || tgtLang) && (
            <>
              {" "}
              · {srcLang ?? "auto"} → {tgtLang ?? "auto"}
            </>
          )}
          {result && (
            <>
              {" "}
              · {result.evaluator} evaluator v{result.version} · {issues.length} findings
            </>
          )}
        </p>
      </div>

      <Card role="region" aria-label="Summary" className="mb-4 gap-0 py-0">
        <CardHeader className="flex items-center gap-2 border-b px-4 py-0 h-10 text-xs">
          <CardTitle className="text-sm font-semibold">Summary</CardTitle>
          {triage.summary_source === "llm" ? <LlmMark model={triage.summary_model} /> : <span className="text-muted-foreground">from rules</span>}
          {computedLocally && (
            <span className="text-muted-foreground" title="This review was stored without triage; threads were computed in the browser with the same rules.">
              · triage computed in browser
            </span>
          )}
        </CardHeader>
        <CardContent className="px-4 py-3 text-sm">
          <Markdown>{triage.summary}</Markdown>
          {(triage.llm_warnings.length > 0 || warnings.length > 0 || notOk.length > 0) && (
            <details className="mt-2 text-[12px] text-shouldfix">
              <summary className="cursor-pointer">
                {triage.llm_warnings.length + warnings.length + notOk.length} warning(s)
              </summary>
              <ul className="list-disc pl-5 mt-1">
                {notOk.map((r) => (
                  <li key={r.evaluator}>
                    {r.evaluator} {r.status}
                    {r.reason ? `: ${r.reason}` : ""}
                  </li>
                ))}
                {warnings.map((w, i) => (
                  <li key={`w${i}`}>{w}</li>
                ))}
                {triage.llm_warnings.map((w, i) => (
                  <li key={`l${i}`}>LLM: {w}</li>
                ))}
              </ul>
            </details>
          )}
        </CardContent>
      </Card>

      <div className="grid gap-4 grid-cols-1 xl:grid-cols-[minmax(0,1fr)_minmax(440px,520px)]">
        {/* Left: players + timeline */}
        <div className="min-w-0 xl:sticky xl:top-16 self-start space-y-3">
          <Players
            ctl={ctl}
            original={origRef ? mediaOf(origRef) : null}
            dub={dubRef ? mediaOf(dubRef) : null}
            onPrev={() => move(-1)}
            onNext={() => move(1)}
          />
          <Card className="p-3 pt-3.5">
            <Timeline
              threads={threads}
              visible={visibleIds}
              selectedId={selectedId}
              duration={duration}
              ctl={ctl}
              onSelect={(id) => select(id, { expand: true })}
            />
          </Card>
          <p className="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-muted-foreground">
            Keys: <Kbd>j</Kbd>/<Kbd>k</Kbd> next/previous thread · <Kbd>o</Kbd> open · <Kbd>space</Kbd> play/pause ·{" "}
            <Kbd>a</Kbd> switch audio · <Kbd>←</Kbd>/<Kbd>→</Kbd> seek 5 s (with Shift: 1 s)
          </p>
          {refImages.length > 0 && (
            <Card role="region" aria-label="Reference images" className="gap-0 p-3">
              <h2 className="text-[12px] font-semibold mb-1.5">
                Reference images <span className="font-normal text-muted-foreground">· not used by the dubbing evaluator yet</span>
              </h2>
              <ul className="flex flex-wrap gap-2">
                {refImages.map((m) => {
                  const u = media[m.asset_id];
                  return (
                    <li key={m.asset_id} className="w-[120px]">
                      {u ? (
                        <a href={u.url} target="_blank" rel="noopener noreferrer">
                          {/* eslint-disable-next-line @next/next/no-img-element -- presigned URL */}
                          <img src={u.url} alt={m.filename ?? "Reference image"} className="w-full aspect-video object-cover rounded border border-border" />
                        </a>
                      ) : (
                        <div className="w-full aspect-video rounded border border-border bg-muted" />
                      )}
                      <p className="text-[11px] text-muted-foreground truncate mt-0.5">{m.filename}</p>
                    </li>
                  );
                })}
              </ul>
            </Card>
          )}
        </div>

        {/* Right: threads */}
        <section aria-label="Review threads" className="min-w-0">
          <Card className="p-3 mb-3">
            <FilterBar
              areaCounts={areaCounts}
              tagCounts={tagCounts}
              filters={filters}
              setFilters={setFilters}
              shown={visible.length}
              total={threads.length}
            />
          </Card>
          {visible.length === 0 && (
            <p className="text-muted-foreground text-[13px] py-6 text-center">
              {threads.length ? "No threads match these filters." : "No findings. Nothing to review."}
            </p>
          )}
          {groups.map(([p, list]) =>
            list.length ? (
              <div key={p} className="mb-4">
                <h2 className={cx("flex items-center gap-2 text-sm font-semibold mb-2", PRIORITY_META[p].fg)}>
                  {PRIORITY_META[p].label}
                  <span className="text-muted-foreground font-normal tabular-nums">{list.length}</span>
                  {p === "needs-human" && <span className="text-muted-foreground font-normal text-[12px]">· low confidence, decide yourself</span>}
                  {p === "nit" && <span className="text-muted-foreground font-normal text-[12px]">· minor, optional</span>}
                </h2>
                <ul className="space-y-1.5">
                  {list.map((t) => (
                    <ThreadItem
                      key={t.id}
                      thread={t}
                      issues={t.issue_ids.map((id) => issuesById.get(id)).filter((x): x is Issue => !!x)}
                      tags={tagsByThread.get(t.id) ?? []}
                      expanded={expanded.has(t.id)}
                      selected={selectedId === t.id}
                      playing={playingId === t.id}
                      fb={fb}
                      send={send}
                      onToggle={toggle}
                      onSeek={seekTo}
                      srcLang={srcLang}
                      tgtLang={tgtLang}
                    />
                  ))}
                </ul>
              </div>
            ) : null,
          )}
          {fbLoadError && (
            <p role="alert" className="text-[12px] text-blocker">
              Could not load saved feedback: {fbLoadError}
            </p>
          )}
        </section>
      </div>
    </div>
  );
}
