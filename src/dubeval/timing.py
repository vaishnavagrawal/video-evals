"""Deterministic timing layer: align original utterances to dubbed speech and flag gaps/lag.

Both tracks share the same video timeline, so no global alignment is needed: a correct dub puts
speech where the original actor speaks. Every decision here is threshold-based and reproducible.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .speech import mask

HOP = 0.01


@dataclass
class Thresholds:
    line_merge_gap: float = 0.35      # VAD segments closer than this form one line
    min_line: float = 0.5             # ignore original blips shorter than this
    match_window: float = 1.0         # dub speech this close to a line may belong to it
    missing_coverage: float = 0.25    # coverage below this => MISSING_DUB
    late_start: float = 0.5
    early_start: float = 0.6
    early_cutoff: float = 0.7
    overrun: float = 0.8
    mid_gap: float = 0.8
    unmatched_min: float = 0.6
    drift_window: int = 5
    drift: float = 0.4
    fast_ratio: float = 1.6           # chars/sec over the dub's own median


@dataclass
class Line:
    index: int
    start: float
    end: float
    dub: list[tuple[float, float]] = field(default_factory=list)
    coverage: float = 0.0

    @property
    def dur(self) -> float:
        return self.end - self.start

    @property
    def dub_start(self) -> float | None:
        return min(s for s, _ in self.dub) if self.dub else None

    @property
    def dub_end(self) -> float | None:
        return max(e for _, e in self.dub) if self.dub else None

    @property
    def onset(self) -> float | None:
        return None if not self.dub else self.dub_start - self.start


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def _runs(m: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index runs where m is True."""
    d = np.diff(np.concatenate([[0], m.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def align(orig: list[tuple[float, float]], dub: list[tuple[float, float]], duration: float,
          t: Thresholds) -> tuple[list[Line], list[tuple[float, float]]]:
    """Assign each dub segment to the original line it overlaps most (or the nearest within window)."""
    lines = [Line(i, s, e) for i, (s, e) in enumerate([x for x in orig if x[1] - x[0] >= t.min_line])]
    for i, ln in enumerate(lines):
        ln.index = i
    unmatched = []
    starts = np.array([ln.start for ln in lines])
    for seg in dub:
        best, best_ov = None, 0.0
        lo = np.searchsorted(starts, seg[0] - 30)
        for ln in lines[lo:]:
            if ln.start > seg[1] + t.match_window:
                break
            ov = _overlap(seg, (ln.start, ln.end))
            gap = 0.0 if ov > 0 else min(abs(seg[0] - ln.end), abs(ln.start - seg[1]))
            score = ov if ov > 0 else -gap
            if gap <= t.match_window and (best is None or score > best_ov):
                best, best_ov = ln, score
        if best is None:
            unmatched.append(seg)
        else:
            best.dub.append(seg)
    dm = mask(dub, duration, HOP)
    for ln in lines:
        a, b = int(ln.start / HOP), int(np.ceil(ln.end / HOP))
        ln.coverage = float(dm[a:b].mean()) if b > a else 0.0
    return lines, unmatched


def _sev(value: float, major: float, critical: float) -> str:
    return "critical" if value >= critical else "major" if value >= major else "minor"


def detect(lines: list[Line], unmatched: list[tuple[float, float]], dub: list[tuple[float, float]],
           orig: list[tuple[float, float]], duration: float, dub_words: list[dict], t: Thresholds) -> list[dict]:
    issues: list[dict] = []
    dm = mask(dub, duration, HOP)
    om = mask(orig, duration, HOP)

    def add(cat, sev, conf, s, e, desc, hint, line=None, **ev):
        issues.append({"category": cat, "source": "timing", "severity": sev, "confidence": round(conf, 2),
                       "start": round(s, 2), "end": round(e, 2), "description": desc, "fix_hint": hint,
                       "line_index": None if line is None else line.index, "evidence": ev})

    for ln in lines:
        ev = {"orig_start": round(ln.start, 2), "orig_end": round(ln.end, 2),
              "dub_start": None if ln.dub_start is None else round(ln.dub_start, 2),
              "dub_end": None if ln.dub_end is None else round(ln.dub_end, 2),
              "coverage": round(ln.coverage, 2)}
        if ln.coverage < t.missing_coverage and ln.dur >= 0.8:
            add("MISSING_DUB", _sev(ln.dur, 1.2, 2.5), 0.9 - ln.coverage, ln.start, ln.end,
                f"Original speech of {ln.dur:.1f}s has only {ln.coverage:.0%} dubbed speech under it.",
                "Generate/insert the dubbed line for this window; it was dropped or placed elsewhere.", ln, **ev)
            continue
        if not ln.dub:
            continue
        onset = ln.onset
        if onset > t.late_start:
            add("LATE_START", _sev(onset, 1.0, 2.0), min(0.95, 0.5 + onset / 3), ln.start, ln.dub_start,
                f"Dub starts {onset:.2f}s after the original actor begins speaking.",
                f"Shift this dubbed line earlier by ~{onset:.2f}s (or trim leading silence).", ln,
                onset_delta=round(onset, 2), **ev)
        elif onset < -t.early_start:
            add("EARLY_START", _sev(-onset, 1.0, 2.0), min(0.9, 0.4 + -onset / 3), ln.dub_start, ln.start,
                f"Dub starts {-onset:.2f}s before the original actor speaks.",
                f"Delay this dubbed line by ~{-onset:.2f}s.", ln, onset_delta=round(onset, 2), **ev)
        tail = ln.end - ln.dub_end
        if tail > t.early_cutoff:
            # actor keeps talking on screen with no dubbed voice
            add("EARLY_CUTOFF", _sev(tail, 1.2, 2.5), min(0.95, 0.5 + tail / 4), ln.dub_end, ln.end,
                f"Dub ends {tail:.2f}s before the original line ends (actor still speaking, no voice).",
                f"Slow this line down or extend translation to fill ~{tail:.2f}s more.", ln, **ev)
        elif -tail > t.overrun:
            spill_into_next = bool(om[int(ln.end / HOP):int(ln.dub_end / HOP)].any())
            add("OVERRUN", _sev(-tail, 1.2, 2.5), min(0.9, 0.4 + -tail / 4), ln.end, ln.dub_end,
                f"Dub keeps talking {-tail:.2f}s after the original line ended"
                + (" and runs into the next line." if spill_into_next else " (over silence/closed mouth)."),
                f"Speed up or shorten the translation by ~{-tail:.2f}s.", ln, spills_into_next=spill_into_next, **ev)
        # silence inside the line while the original is speaking
        a = int(max(ln.start, ln.dub_start) / HOP)
        b = int(min(ln.end, ln.dub_end) / HOP)
        if b > a:
            for r0, r1 in _runs(~dm[a:b] & om[a:b]):
                g = (r1 - r0) * HOP
                if g >= t.mid_gap:
                    s = (a + r0) * HOP
                    add("MID_LINE_GAP", _sev(g, 1.2, 2.0), min(0.9, 0.45 + g / 4), s, s + g,
                        f"{g:.2f}s silence in the dub while the original actor is speaking.",
                        "Re-time the segments around this pause or fill it with the missing phrase.", ln, **ev)

    for s, e in unmatched:
        if e - s >= t.unmatched_min:
            add("UNMATCHED_SPEECH", _sev(e - s, 1.5, 3.0), 0.6, s, e,
                f"{e - s:.2f}s of dubbed speech where the original has no speech nearby.",
                "Check if this line belongs to another timestamp or is a hallucinated/duplicated TTS line.")

    # sustained offset: rolling median of onset deltas
    onsets = [(ln, ln.onset) for ln in lines if ln.onset is not None and ln.coverage >= t.missing_coverage]
    w = t.drift_window
    i = 0
    while i + w <= len(onsets):
        med = float(np.median([o for _, o in onsets[i:i + w]]))
        if abs(med) >= t.drift:
            j = i + w
            while j < len(onsets) and abs(float(np.median([o for _, o in onsets[j - w + 1:j + 1]]))) >= t.drift:
                j += 1
            seg = onsets[i:j]
            m = float(np.median([o for _, o in seg]))
            add("SYNC_DRIFT", _sev(abs(m), 0.7, 1.2), 0.75, seg[0][0].start, seg[-1][0].end,
                f"Dub is consistently {'late' if m > 0 else 'early'} by ~{abs(m):.2f}s over {len(seg)} lines.",
                f"Apply a {-m:+.2f}s offset to dubbed lines {seg[0][0].index}-{seg[-1][0].index}.",
                median_onset_delta=round(m, 2), lines=[ln.index for ln, _ in seg])
            i = j
        else:
            i += 1

    # speaking-rate cramming relative to the dub's own median (needs ASR words)
    if dub_words:
        rates = []
        for ln in lines:
            if not ln.dub:
                continue
            ws = [x for x in dub_words if ln.dub_start <= (x["start"] + x["end"]) / 2 < ln.dub_end]
            span = ln.dub_end - ln.dub_start
            if len(ws) >= 3 and span > 0.8:
                rates.append((ln, sum(len(x["word"]) for x in ws) / span))
        if rates:
            med = float(np.median([r for _, r in rates]))
            for ln, r in rates:
                if r > med * t.fast_ratio:
                    add("FAST_SPEECH", "minor" if r < med * 2 else "major", 0.55, ln.dub_start, ln.dub_end,
                        f"Dub speaks {r:.1f} chars/s vs a median of {med:.1f} (x{r / med:.1f}); likely time-squeezed.",
                        "Use a shorter translation instead of speeding up the TTS.", ln,
                        chars_per_sec=round(r, 1), median_chars_per_sec=round(med, 1))
    return issues


def stats(lines: list[Line], orig: list[tuple[float, float]], dub: list[tuple[float, float]],
          duration: float) -> dict:
    om, dm = mask(orig, duration, HOP), mask(dub, duration, HOP)
    onsets = [ln.onset for ln in lines if ln.onset is not None and ln.coverage >= 0.25]
    return {
        "orig_speech_seconds": round(om.sum() * HOP, 1),
        "dub_speech_seconds": round(dm.sum() * HOP, 1),
        "speech_overlap_ratio": round(float((om & dm).sum() / max(1, om.sum())), 3),
        "unmatched_dub_ratio": round(float((dm & ~om).sum() / max(1, dm.sum())), 3),
        "median_onset_delta": round(float(np.median(onsets)), 3) if onsets else None,
        "p90_abs_onset_delta": round(float(np.percentile(np.abs(onsets), 90)), 3) if onsets else None,
    }
