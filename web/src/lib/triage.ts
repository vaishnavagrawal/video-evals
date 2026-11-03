// Client-side port of the contract's deterministic triage rules (docs/triage-and-ui-contract.md, section 1).
// Used only by the mock API to synthesize `triage` for fixtures; the real backend computes it in vreview.triage.
import {
  PRIORITY_RANK,
  type Area,
  type Issue,
  type Priority,
  type PriorityCounts,
  type Review,
  type Thread,
  type Triage,
  type Verdict,
} from "./api/types";

export const TRIAGE_CONFIG = {
  semanticMinConfidence: 0.6,
  minConfidence: 0.4,
  mergeGap: 0.3,
  maxThreadSpan: 15,
};

const AREA_BY_CATEGORY: Record<string, Area> = {};
const AREAS: [Area, string[]][] = [
  ["accuracy", ["MEANING_MISMATCH", "MISSING_CONTENT", "ADDED_CONTENT", "NAME_OR_TERM_ERROR"]],
  [
    "sync",
    [
      "LATE_START",
      "EARLY_START",
      "EARLY_CUTOFF",
      "OVERRUN",
      "SYNC_DRIFT",
      "MID_LINE_GAP",
      "MISSING_DUB",
      "UNMATCHED_SPEECH",
      "DURATION_MISMATCH",
    ],
  ],
  ["audio-mix", ["BACKGROUND_DROPOUT", "ENERGY_MISMATCH"]],
  ["delivery", ["TONE_MISMATCH", "FAST_SPEECH"]],
  ["script", ["SCRIPT_DEVIATION", "SUBTITLE_AUDIO_DESYNC"]],
  ["language", ["WRONG_LANGUAGE", "UNTRANSLATED"]],
  ["video", ["VIDEO_MISMATCH"]],
];
for (const [area, cats] of AREAS) for (const c of cats) AREA_BY_CATEGORY[c] = area;

export const areaOf = (category: string): Area => AREA_BY_CATEGORY[category] ?? "other";

export function priorityOf(issue: Issue): Priority {
  const src = issue.details?.source;
  if (src === "language" && issue.severity === "critical") return "blocker";
  if (src === "semantic" && issue.confidence < TRIAGE_CONFIG.semanticMinConfidence) return "needs-human";
  if (issue.confidence < TRIAGE_CONFIG.minConfidence) return "needs-human";
  return issue.severity === "critical" ? "blocker" : issue.severity === "major" ? "should-fix" : "nit";
}

export function tagsOf(issue: Issue): string[] {
  const tags = [`area:${areaOf(issue.category)}`];
  if (issue.details?.visual?.speaking_likely === true) tags.push("on-camera");
  if (issue.details?.source === "semantic") tags.push("llm");
  return tags;
}

const maxPriority = (ps: Priority[]): Priority =>
  ps.reduce((a, b) => (PRIORITY_RANK[b] > PRIORITY_RANK[a] ? b : a), "nit" as Priority);

export const CATEGORY_TITLES: Record<string, string> = {
  MEANING_MISMATCH: "Translation changes the meaning",
  MISSING_CONTENT: "Translation drops content",
  ADDED_CONTENT: "Translation adds content",
  NAME_OR_TERM_ERROR: "Name or term is wrong",
  LATE_START: "Dub starts late",
  EARLY_START: "Dub starts before the original line",
  EARLY_CUTOFF: "Dub cuts off before the original line ends",
  OVERRUN: "Dub runs past the end of the original line",
  SYNC_DRIFT: "Dub drifts out of sync",
  MID_LINE_GAP: "Gap in the middle of a dubbed line",
  MISSING_DUB: "Original line has no dub",
  UNMATCHED_SPEECH: "Dub speech with no matching original line",
  DURATION_MISMATCH: "Dub duration differs from the original",
  BACKGROUND_DROPOUT: "Background audio drops out",
  ENERGY_MISMATCH: "Dub energy differs from the original",
  TONE_MISMATCH: "Delivery tone differs from the original",
  FAST_SPEECH: "Dub speech is rushed",
  SCRIPT_DEVIATION: "Dub deviates from the script",
  SUBTITLE_AUDIO_DESYNC: "Subtitles out of sync with the dub",
  WRONG_LANGUAGE: "Dub is in the wrong language",
  UNTRANSLATED: "Line left untranslated",
  VIDEO_MISMATCH: "Video differs from the original",
};

const fmtT = (t: number) => {
  const m = Math.floor(t / 60);
  return `${m}:${(t % 60).toFixed(1).padStart(4, "0")}`;
};

function ruleComment(members: Issue[]): string {
  return members
    .map(
      (i) =>
        `- **${i.category}** (${i.severity}, ${fmtT(i.t0)}–${fmtT(i.t1)}): ${i.description}\n  Fix: ${i.fix_hint}`,
    )
    .join("\n");
}

function ruleTitle(members: Issue[], priority: Priority): string {
  const top = members.find((m) => m.priority === priority) ?? members[0];
  const base = CATEGORY_TITLES[top.category] ?? top.category.toLowerCase().replace(/_/g, " ");
  const others = new Set(members.map((m) => m.category));
  others.delete(top.category);
  return others.size ? `${base} (+${others.size} more)` : base;
}

/** Mirrors vreview.triage.triage(): returns the triage and sets priority/tags/area/thread_id on each issue. */
export function triage(review: Review): Triage {
  const issues = review.results.flatMap((r) => r.issues);
  for (const i of issues) {
    i.area = areaOf(i.category);
    i.priority = priorityOf(i);
    i.tags = tagsOf(i);
  }

  // Cluster per track: overlapping or within mergeGap, capped at maxThreadSpan.
  const byTrack = new Map<string, Issue[]>();
  for (const i of issues) {
    const k = i.track ?? "";
    if (!byTrack.has(k)) byTrack.set(k, []);
    byTrack.get(k)!.push(i);
  }
  const clusters: Issue[][] = [];
  for (const list of byTrack.values()) {
    list.sort((a, b) => a.t0 - b.t0 || a.t1 - b.t1);
    let cur: Issue[] = [];
    let t0 = 0;
    let t1 = 0;
    for (const i of list) {
      if (cur.length && i.t0 <= t1 + TRIAGE_CONFIG.mergeGap && Math.max(t1, i.t1) - t0 <= TRIAGE_CONFIG.maxThreadSpan) {
        cur.push(i);
        t1 = Math.max(t1, i.t1);
      } else {
        if (cur.length) clusters.push(cur);
        cur = [i];
        t0 = i.t0;
        t1 = i.t1;
      }
    }
    if (cur.length) clusters.push(cur);
  }
  clusters.sort((a, b) => a[0].t0 - b[0].t0 || (a[0].track ?? "").localeCompare(b[0].track ?? ""));

  const threads: Thread[] = clusters.map((members, idx) => {
    const id = `T${String(idx + 1).padStart(3, "0")}`;
    for (const m of members) m.thread_id = id;
    const priority = maxPriority(members.map((m) => m.priority!));
    const areas = [...new Set(members.map((m) => m.area!))];
    const withText = members.find((m) => m.details?.original_text || m.details?.dubbed_text);
    return {
      id,
      track: members[0].track ?? null,
      t0: Math.min(...members.map((m) => m.t0)),
      t1: Math.max(...members.map((m) => m.t1)),
      priority,
      areas,
      issue_ids: members.map((m) => m.id),
      title: ruleTitle(members, priority),
      comment: ruleComment(members),
      comment_source: "rule",
      original_text: withText?.details.original_text ?? null,
      dubbed_text: withText?.details.dubbed_text ?? null,
    };
  });

  const counts: PriorityCounts = { blocker: 0, "should-fix": 0, "needs-human": 0, nit: 0 };
  for (const t of threads) counts[t.priority]++;
  // Contract decision: area_counts counts threads (a thread counts once for each of its areas).
  const area_counts: Partial<Record<Area, number>> = {};
  for (const t of threads) for (const a of t.areas) area_counts[a] = (area_counts[a] ?? 0) + 1;

  const verdict: Verdict = counts.blocker ? "request_changes" : counts["needs-human"] ? "needs_human" : "approve";
  const topAreas = Object.entries(area_counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([a, n]) => `${a} (${n})`)
    .join(", ");
  const verdictText =
    verdict === "request_changes"
      ? "Changes requested"
      : verdict === "needs_human"
        ? "Needs a human decision"
        : "Approved";
  const summary =
    `${verdictText}: ${counts.blocker} blocker, ${counts["should-fix"]} should-fix, ` +
    `${counts["needs-human"]} needs-human and ${counts.nit} nit threads from ${issues.length} issues. ` +
    (topAreas ? `Most threads are about ${topAreas}.` : "");

  return {
    version: "1",
    verdict,
    counts,
    area_counts,
    threads,
    summary,
    summary_source: "rule",
    llm_warnings: [],
  };
}
