"""Code-review style triage over evaluator issues (contract: docs/triage-and-ui-contract.md, sections 1-2).

Pure and deterministic: no I/O, no LLM. `triage()` never changes `severity`, `t0`/`t1` or the score. It
labels each issue (priority, area, tags, thread), clusters issues into threads (one thread = one review
comment), and writes rule-based text for every thread and for the review. An LLM may replace that text
later (`vreview.triage_llm`), but the rule text alone always makes a complete review.

`Thread` and `Triage` live in `vreview.schema` (so `Review.triage` can use them without a circular import)
and are re-exported here.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from pydantic import BaseModel, ConfigDict, Field

from vreview.schema import Area, Issue, Priority, Review, Thread, Triage, Verdict

__all__ = [
    "AREA_BY_CATEGORY",
    "PRIORITIES",
    "Thread",
    "Triage",
    "TriageConfig",
    "issue_area",
    "issue_priority",
    "issue_tags",
    "triage",
]

# Highest first. A thread takes the best priority among its issues.
PRIORITIES: tuple[Priority, ...] = ("blocker", "should-fix", "needs-human", "nit")
_PRIORITY_RANK = {p: n for n, p in enumerate(PRIORITIES)}
_SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2}
_SEVERITY_PRIORITY: dict[str, Priority] = {"critical": "blocker", "major": "should-fix", "minor": "nit"}

_AREA_CATEGORIES: dict[Area, tuple[str, ...]] = {
    "accuracy": ("MEANING_MISMATCH", "MISSING_CONTENT", "ADDED_CONTENT", "NAME_OR_TERM_ERROR"),
    "sync": ("LATE_START", "EARLY_START", "EARLY_CUTOFF", "OVERRUN", "SYNC_DRIFT", "MID_LINE_GAP",
             "MISSING_DUB", "UNMATCHED_SPEECH", "DURATION_MISMATCH", "LIP_SYNC"),
    "audio-mix": ("BACKGROUND_DROPOUT", "ENERGY_MISMATCH", "AUDIO_ARTIFACT"),
    "delivery": ("TONE_MISMATCH", "FAST_SPEECH", "EMOTION_MISMATCH", "VOICE_MISMATCH"),
    "script": ("SCRIPT_DEVIATION", "SUBTITLE_AUDIO_DESYNC", "BRIEF_MISMATCH"),
    "language": ("WRONG_LANGUAGE", "UNTRANSLATED"),
    "video": ("VIDEO_MISMATCH", "VISUAL_ARTIFACT"),
}
AREA_BY_CATEGORY: dict[str, Area] = {cat: area for area, cats in _AREA_CATEGORIES.items() for cat in cats}

_EPS = 1e-9  # float slack for the gap / span comparisons


class TriageConfig(BaseModel):
    """Thresholds for the priority rule and the thread clustering."""
    model_config = ConfigDict(frozen=True)

    semantic_min_confidence: float = Field(default=0.6, description="LLM-judged issues below this -> needs-human")
    min_confidence: float = Field(default=0.4, description="Any issue below this -> needs-human")
    thread_gap_s: float = Field(default=0.3, ge=0, description="Issues this close (or overlapping) share a thread")
    thread_max_span_s: float = Field(default=15.0, gt=0, description="A thread never grows past this span")


# ---------------------------------------------------------------------------
# Per-issue labels
# ---------------------------------------------------------------------------

def _source(issue: Issue) -> str | None:
    return (issue.details or {}).get("source")


def issue_priority(issue: Issue, cfg: TriageConfig) -> Priority:
    """Rules in order; the first that applies wins.

    1. language-sourced + critical -> blocker (wrong/untranslated language is never ambiguous)
    2. LLM-sourced (source "semantic") with confidence < semantic_min_confidence -> needs-human
    3. any issue with confidence < min_confidence -> needs-human
    4. by severity: critical -> blocker, major -> should-fix, minor -> nit

    The visual "is a face speaking" adjustment is already folded into `severity` by dubeval.visual.adjust.
    """
    source = _source(issue)
    if source == "language" and issue.severity == "critical":
        return "blocker"
    if source == "semantic" and issue.confidence < cfg.semantic_min_confidence:
        return "needs-human"
    if issue.confidence < cfg.min_confidence:
        return "needs-human"
    return _SEVERITY_PRIORITY[issue.severity]


def issue_area(issue: Issue) -> Area:
    return AREA_BY_CATEGORY.get(issue.category, "other")


def issue_tags(issue: Issue) -> list[str]:
    tags = [f"area:{issue_area(issue)}"]
    visual = (issue.details or {}).get("visual")
    if isinstance(visual, dict) and visual.get("speaking_likely") is True:
        tags.append("on-camera")
    if _source(issue) == "semantic":
        tags.append("llm")  # LLM findings were only 18-25% repeatable across runs; the UI says so
    if _source(issue) == "av_judge":
        tags.append("av-judge")
    review = (issue.details or {}).get("av_review")
    if isinstance(review, dict) and review.get("verdict") in ("confirm", "dispute"):
        tags.append("av:confirmed" if review["verdict"] == "confirm" else "av:disputed")
    return tags


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------

def _end(issue: Issue) -> float:
    return max(issue.t0, issue.t1)


def _cluster(issues: list[Issue], cfg: TriageConfig) -> list[list[Issue]]:
    """Per track: sort by time, then chain issues whose intervals overlap or sit within `thread_gap_s`.
    A thread is closed once adding the next issue would stretch it beyond `thread_max_span_s`."""
    by_track: dict[str | None, list[Issue]] = defaultdict(list)
    for issue in issues:
        by_track[issue.track].append(issue)

    clusters: list[list[Issue]] = []
    for items in by_track.values():
        items.sort(key=lambda i: (i.t0, _end(i), i.id))
        current = [items[0]]
        start, end = items[0].t0, _end(items[0])
        for issue in items[1:]:
            close_enough = issue.t0 <= end + cfg.thread_gap_s + _EPS
            within_cap = max(end, _end(issue)) - start <= cfg.thread_max_span_s + _EPS
            if close_enough and within_cap:
                current.append(issue)
                end = max(end, _end(issue))
            else:
                clusters.append(current)
                current = [issue]
                start, end = issue.t0, _end(issue)
        clusters.append(current)

    clusters.sort(key=lambda c: (c[0].t0, c[0].track or "", c[0].id))
    return clusters


def _importance(issue: Issue) -> tuple:
    """Sort key, most important first: priority, severity, confidence, then time."""
    return (_PRIORITY_RANK[issue.priority or "nit"], _SEVERITY_RANK[issue.severity], -issue.confidence,
            issue.t0, issue.id)


def _join_texts(issues: list[Issue], key: str) -> str | None:
    parts: list[str] = []
    for issue in issues:  # time order
        text = ((issue.details or {}).get(key) or "").strip()
        if text and not any(text in p for p in parts):
            parts.append(text)
    return " ".join(parts) or None


# ---------------------------------------------------------------------------
# Rule-based text
# ---------------------------------------------------------------------------

_TITLES = {
    "MEANING_MISMATCH": "Dub changes the meaning of the line",
    "MISSING_CONTENT": "Dub leaves out part of the original line",
    "ADDED_CONTENT": "Dub adds content that is not in the original",
    "NAME_OR_TERM_ERROR": "Name or term is wrong in the dub",
    "LATE_START": "Dub starts late",
    "EARLY_START": "Dub starts before the original line",
    "EARLY_CUTOFF": "Dub cuts off while the actor is still speaking",
    "OVERRUN": "Dub keeps talking after the original line ends",
    "SYNC_DRIFT": "Dub timing drifts away from the original",
    "MID_LINE_GAP": "Dub goes silent in the middle of a line",
    "MISSING_DUB": "Original line has no dubbed speech",
    "UNMATCHED_SPEECH": "Dubbed speech where the original is silent",
    "DURATION_MISMATCH": "Dub and original differ in length",
    "BACKGROUND_DROPOUT": "Music or effects bed drops out in the dub",
    "ENERGY_MISMATCH": "Dub delivery energy does not match the original",
    "TONE_MISMATCH": "Dub tone does not match the original",
    "FAST_SPEECH": "Dub is rushed to fit the timing",
    "SCRIPT_DEVIATION": "Spoken dub differs from its own subtitle script",
    "SUBTITLE_AUDIO_DESYNC": "Subtitles and dubbed audio are out of sync",
    "WRONG_LANGUAGE": "Dub is not in the target language",
    "UNTRANSLATED": "Line was left untranslated",
    "VIDEO_MISMATCH": "Video does not match the original",
    "LIP_SYNC": "Lips do not match the speech",
    "VOICE_MISMATCH": "Voice does not fit the on-screen speaker",
    "EMOTION_MISMATCH": "Delivery misses the emotion of the scene",
    "AUDIO_ARTIFACT": "Audible artifact in the voice or mix",
    "VISUAL_ARTIFACT": "Visual glitch or artifact",
    "BRIEF_MISMATCH": "Content does not match the brief",
}

_WHY = {
    "accuracy": "Viewers get a different message than the original speaker gave.",
    "sync": "The dub no longer lines up with the speaker, so lines land late, get cut short or collide with the next one.",
    "audio-mix": "The sound bed differs from the original, so the scene feels flat or the mix jumps.",
    "delivery": "The performance does not carry the pace or emotion of the original.",
    "script": "The audio does not match the script it ships with, so subtitles read wrongly against it.",
    "language": "Viewers hear the wrong language, or one that was never translated.",
    "video": "The picture no longer matches the original.",
    "other": "An evaluator flagged this and it affects how the dub plays.",
}

_AREA_LABELS = {"audio-mix": "audio mix"}
_VERDICT_LABELS = {
    "request_changes": "Changes requested",
    "needs_human": "Needs a human look",
    "approve": "Approved",
}
_TEXT_LIMIT = 400
_RELATED_SUFFIX = re.compile(r" \(\+\d+ related\)$")


def _tc(t: float) -> str:
    """Timecode for prose: M:SS.s, or H:MM:SS.s past an hour."""
    minutes, seconds = divmod(max(t, 0.0), 60)
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}:{minutes:02d}:{seconds:04.1f}" if hours else f"{minutes}:{seconds:04.1f}"


def _clip(text: str, limit: int = _TEXT_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _title(ranked: list[Issue]) -> str:
    primary = ranked[0]
    title = _TITLES.get(primary.category) or primary.category.replace("_", " ").capitalize()
    return f"{title} (+{len(ranked) - 1} related)" if len(ranked) > 1 else title


def _comment(ranked: list[Issue], priority: Priority, original: str | None, dubbed: str | None) -> str:
    """Markdown with three parts: what is wrong, why it matters, how to fix. `ranked` is importance-sorted."""
    primary = ranked[0]
    descriptions = _unique([m.description for m in ranked])
    fixes = _unique([m.fix_hint for m in ranked])

    lines: list[str] = []
    if len(descriptions) == 1:
        lines.append(f"**What:** {descriptions[0]}")
    else:
        lines.append("**What:**")
        lines += [f"- {d}" for d in descriptions]

    why = _WHY[issue_area(primary)]
    if "on-camera" in primary.tags:
        why += " A speaking face is visible here, so the mismatch is easy to see."
    lines += ["", f"**Why it matters:** {why}"]

    if len(fixes) == 1:
        lines += ["", f"**Fix:** {fixes[0]}"]
    elif fixes:
        lines += ["", "**Fix:**"] + [f"- {f}" for f in fixes]

    if priority == "needs-human":
        low = min(m.confidence for m in ranked)
        lines += ["", f"**Needs a human:** the evaluator is unsure here (confidence {low:.2f}). "
                      "Listen to this segment before acting."]
    elif any("llm" in m.tags for m in ranked):
        lines += ["", "_Judged by an LLM, whose findings repeat in only about 1 run in 4. Confirm by listening._"]

    quotes = []
    if original:
        quotes.append(f"> **Original:** {_clip(original)}")
    if dubbed:
        quotes.append(f"> **Dub:** {_clip(dubbed)}")
    if quotes:
        lines += ["", "  \n".join(quotes)]
    return "\n".join(lines)


def _build_thread(thread_id: str, members: list[Issue]) -> Thread:
    """`members` is in time order."""
    ranked = sorted(members, key=_importance)
    priority = ranked[0].priority or "nit"
    original = _join_texts(members, "original_text")
    dubbed = _join_texts(members, "dubbed_text")
    areas: list[Area] = []
    for m in ranked:
        area = issue_area(m)
        if area not in areas:
            areas.append(area)
    return Thread(
        id=thread_id,
        track=ranked[0].track,
        t0=min(m.t0 for m in members),
        t1=max(_end(m) for m in members),
        priority=priority,
        areas=areas,
        issue_ids=[m.id for m in members],
        title=_title(ranked),
        comment=_comment(ranked, priority, original, dubbed),
        comment_source="rule",
        original_text=original,
        dubbed_text=dubbed,
    )


def _verdict(counts: dict[str, int], any_evaluator_ok: bool, incomplete: bool = False) -> Verdict:
    """request_changes if any blocker thread, else needs_human if any needs-human thread, else approve.
    A review in which no evaluator completed (all skipped or failed, or none ran) checked nothing, so it is
    never approved: it goes to a human. Neither is a review with gaps (`incomplete`, e.g. judge calls that
    failed): missing findings must not read as a clean result."""
    if not any_evaluator_ok:
        return "needs_human"
    if counts["blocker"]:
        return "request_changes"
    if counts["needs-human"] or incomplete:
        return "needs_human"
    return "approve"


def incomplete_reasons(review: Review) -> list[str]:
    return [f"{r.evaluator}: {why}" for r in review.results if r.status == "ok" for why in r.incomplete]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _lead_sentence(threads: list[Thread], counts: dict[str, int]) -> str | None:
    """Name the most common problems in the highest tier that must be acted on (blocker, else should-fix)."""
    tier = next((p for p in ("blocker", "should-fix") if counts[p]), None)
    if tier is None:
        return None
    groups: dict[str, list[Thread]] = {}
    for thread in (t for t in threads if t.priority == tier):
        groups.setdefault(_RELATED_SUFFIX.sub("", thread.title), []).append(thread)
    top = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[1][0].t0))[:2]
    parts = [f"{title[0].lower()}{title[1:]} ({_plural(len(ts), 'thread')}, first at {_tc(ts[0].t0)})"
             for title, ts in top]
    return f"The {tier} threads are mostly: {', '.join(parts)}."


def _summary(review: Review, threads: list[Thread], n_issues: int, counts: dict[str, int],
             area_counts: dict[str, int], verdict: Verdict) -> str:
    """At most 5 sentences of markdown."""
    sentences: list[str] = []
    not_ok = [f"{r.evaluator} ({r.status})" for r in review.results if r.status != "ok"]
    if len(not_ok) == len(review.results):
        sentences.append(f"**{_VERDICT_LABELS[verdict]}.** No evaluator completed, so nothing in this video was checked.")
    if not_ok:
        sentences.append(f"Did not complete: {', '.join(not_ok)}.")

    if threads:
        by_priority = ", ".join(f"{counts[p]} {p}" for p in PRIORITIES if counts[p])
        tracks = len({t.track for t in threads})
        scope = f" on {_plural(tracks, 'track')}" if tracks > 1 else ""
        sentences.append(
            f"**{_VERDICT_LABELS[verdict]}.** {_plural(len(threads), 'thread')} from "
            f"{_plural(n_issues, 'issue')}{scope}: {by_priority}."
        )
        top = [f"{_AREA_LABELS.get(area, area)} ({n})" for area, n in list(area_counts.items())[:3]]
        sentences.append("Most threads are about " + ", then ".join(top) + ".")
        lead = _lead_sentence(threads, counts)
        if lead:
            sentences.append(lead)
    elif len(not_ok) < len(review.results):
        sentences.append(f"**{_VERDICT_LABELS[verdict]}.** No issues were found.")

    gaps = incomplete_reasons(review)
    if gaps:
        sentences.insert(0, f"**Incomplete review.** {gaps[0]}" + (f" (+{len(gaps) - 1} more)" if len(gaps) > 1 else "")
                         + "; findings for those parts are missing.")
    if review.score is not None:
        score = f"Score {review.score:.1f}/100"
        if verdict == "approve" and review.score < 70:
            score += " counts every detected issue, including nits; the verdict only weighs blocker and needs-human threads"
        sentences.append(score + ".")
    return " ".join(sentences[:6])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def triage(review: Review, cfg: TriageConfig | None = None) -> Triage:
    """Label and cluster every issue of `review` and return the Triage.

    Sets `priority`, `tags`, `area` and `thread_id` on each issue in place (so calling it twice gives the
    same result) but does not assign `review.triage`; the caller stores the return value.
    """
    cfg = cfg or TriageConfig()
    issues = [i for result in review.results for i in result.issues]

    for issue in issues:
        issue.priority = issue_priority(issue, cfg)
        issue.area = issue_area(issue)
        issue.tags = issue_tags(issue)
        issue.thread_id = None

    threads: list[Thread] = []
    for n, members in enumerate(_cluster(issues, cfg) if issues else [], start=1):
        thread_id = f"T{n:03d}"
        for issue in members:
            issue.thread_id = thread_id
        threads.append(_build_thread(thread_id, members))

    counts = {p: 0 for p in PRIORITIES}
    areas: Counter[str] = Counter()
    for thread in threads:
        counts[thread.priority] += 1
        areas.update(thread.areas)
    area_counts = dict(sorted(areas.items(), key=lambda kv: (-kv[1], kv[0])))
    verdict = _verdict(counts, any(r.status == "ok" for r in review.results), bool(incomplete_reasons(review)))

    return Triage(
        verdict=verdict,
        counts=counts,
        area_counts=area_counts,
        threads=threads,
        summary=_summary(review, threads, len(issues), counts, area_counts, verdict),
        summary_source="rule",
    )
