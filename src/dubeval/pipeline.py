"""Orchestrates the layers into structured reports, one per (original track, dub track) pair.

  tracks: probe -> extract -> language-ID -> pick original + dub streams (skip passthrough copies)
  per pair:
    VAD (both) -> align lines -> timing issues
    [stems] -> background/energy issues
    ASR (both, cached per track) -> fast-speech + LLM semantic judge on aligned lines
    subtitle-vs-audio check (subtitle stream matching the dub language)
    visual mouth check on flagged windows -> severity re-weighting
    dedupe/rank -> Report

Nothing here is language specific: languages come from `lang.resolve` (declared) or Whisper LID
(detected), and per-track artefacts (audio, LID, ASR, stems) are cached so an original shared by
n dubs is processed once.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import audio, speech, timing, visual
from .lang import same_language
from .media import extract_subtitles, parse_srt, probe_cached, tc
from .schema import AlignedLine, BatchReport, Issue, Report, Summary
from .tracks import Track, dub_tracks, original_track

log = logging.getLogger("dubeval")

SEV_WEIGHT = {"critical": 6.0, "major": 2.5, "minor": 0.7}


@dataclass
class Config:
    src_lang: str | None = None                          # None = detect
    tgt_langs: list[str] = field(default_factory=list)   # [] = evaluate every non-source dub stream
    orig_audio: int | None = None
    dub_audio: int | None = None
    asr_cloud_model: str | None = "groq/whisper-large-v3"
    asr_local_model: str = "small"
    llm_models: list[str] = field(default_factory=lambda: ["gemini-flash", "auto/best-fast"])
    separate: bool = False
    visual: bool = True
    max_visual_windows: int = 60
    threads: int = 4
    thresholds: timing.Thresholds = field(default_factory=timing.Thresholds)


def _clock(timings: dict, name: str, t0: float) -> float:
    now = time.perf_counter()
    timings[name] = round(now - t0, 2)
    log.info("stage %-10s %6.1fs", name, now - t0)
    return now


def _subtitle_issues(cues: list[dict], dub_segs, duration: float) -> list[dict]:
    """Subtitle cue with no dubbed speech under it."""
    dm = speech.mask(dub_segs, duration, timing.HOP)
    out = []
    for c in cues:
        a, b = int(c["start"] / timing.HOP), int(np.ceil(c["end"] / timing.HOP))
        if b <= a or c["end"] - c["start"] < 0.6:
            continue
        cov = float(dm[a:b].mean())
        if cov < 0.15:
            out.append({"category": "SUBTITLE_AUDIO_DESYNC", "source": "subtitle", "severity": "minor",
                        "confidence": 0.6, "start": c["start"], "end": c["end"],
                        "description": f"Subtitle cue is shown but only {cov:.0%} of it has dubbed speech.",
                        "fix_hint": "Re-time the subtitle cue to the dubbed audio, or the line audio is missing.",
                        "dubbed_text": c["text"], "evidence": {"coverage": round(cov, 2)}})
    return out


def _subtitle_stream(dub: Track) -> int | None:
    """Subtitle stream in the dub's language; a lone untagged/other stream only if it is the only one."""
    subs = probe_cached(dub.path).subtitles
    for s in subs:
        if same_language(s.get("lang"), dub.lang.base):
            return s["index"]
    if len(subs) == 1 and not subs[0].get("lang"):
        return subs[0]["index"]
    return None


def _language_issues(src: Track, dub: Track, duration: float) -> list[dict]:
    """Spoken language of the dub track vs what it should be (detected by Whisper LID)."""
    det, prob = dub.lid.get("lang"), dub.lid.get("prob", 0.0)
    if not det or prob < 0.5:
        return []
    ev = {"detected": dub.lid, "expected": dub.lang.tag, "source_language": src.lang.tag}
    if same_language(det, src.lang.base) and not same_language(dub.lang.base, src.lang.base):
        return [{"category": "UNTRANSLATED", "source": "language", "severity": "critical", "confidence": prob,
                 "start": 0.0, "end": duration,
                 "description": f"Dub track is spoken in the source language ({det}, p={prob:.2f}), not {dub.lang.name}.",
                 "fix_hint": "Wrong audio stream was muxed or TTS ran in the source language; re-mux the dub.",
                 "evidence": ev}]
    if not same_language(det, dub.lang.base):
        return [{"category": "WRONG_LANGUAGE", "source": "language", "severity": "critical", "confidence": prob,
                 "start": 0.0, "end": duration,
                 "description": f"Dub track sounds like '{det}' (p={prob:.2f}) but is declared {dub.lang.name}.",
                 "fix_hint": f"Regenerate the dub in {dub.lang.name} ({dub.lang.tag}) or fix the language label.",
                 "evidence": ev}]
    return []


def _dedupe(issues: list[dict]) -> list[dict]:
    """Same category on overlapping windows -> keep the most severe."""
    order = {"critical": 2, "major": 1, "minor": 0}
    issues.sort(key=lambda x: (x["category"], x["start"]))
    kept: list[dict] = []
    for it in issues:
        prev = kept[-1] if kept else None
        if prev and prev["category"] == it["category"] and it["start"] < prev["end"] - 0.05:
            if order[it["severity"]] > order[prev["severity"]]:
                kept[-1] = it
            continue
        kept.append(it)
    return kept


def run_pair(src: Track, dub: Track, work: Path, cfg: Config) -> Report:
    pair_dir = work / "pairs" / f"{src.slug}__{dub.slug}"
    pair_dir.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    warnings: list[str] = []
    t = time.perf_counter()
    log.info("pair %s -> %s (%s)", src.lang.tag, dub.lang.tag, dub.slug)

    po, pd = probe_cached(src.path), probe_cached(dub.path)
    duration = min(po.duration, pd.duration)
    issues: list[dict] = _language_issues(src, dub, duration)
    if abs(po.duration - pd.duration) > 0.5:
        issues.append({"category": "DURATION_MISMATCH", "source": "video", "severity": "major", "confidence": 0.95,
                       "start": duration, "end": max(po.duration, pd.duration),
                       "description": f"Durations differ: original {po.duration:.2f}s vs dub {pd.duration:.2f}s.",
                       "fix_hint": "Keep the dubbed mix exactly as long as the original timeline.", "evidence": {}})

    # ---- stems (optional) then VAD: VAD on vocal stems is far cleaner under music
    ov, ob, s1 = src.stems(cfg.separate, cfg.threads, warnings)
    dv, db, s2 = dub.stems(cfg.separate, cfg.threads, warnings)
    stems = s1 and s2
    if not stems:  # never mix a stem with a raw mix
        ov, ob, dv, db = src.x, src.x, dub.x, dub.x
    t = _clock(timings, "stems", t)
    o_raw = speech.vad_segments(ov)
    d_raw = speech.vad_segments(dv)
    tt = cfg.thresholds
    lines, unmatched = timing.align(speech.merge(o_raw, tt.line_merge_gap), d_raw, duration, tt)
    t = _clock(timings, "vad_align", t)

    asr_o, asr_d = src.asr(cfg, warnings), dub.asr(cfg, warnings)
    t = _clock(timings, "asr", t)

    # chars/sec is only comparable for spaced scripts with ASR words; median-relative so per-language rate cancels
    issues += timing.detect(lines, unmatched, d_raw, o_raw, duration, asr_d["words"], tt)
    bg, bg_offset = audio.background_check(ob, db, o_raw, d_raw, duration, stems)
    issues += bg
    issues += audio.energy_mismatch(lines, ov, dv)
    if bg_offset is not None and abs(bg_offset) > 40:
        issues.append({"category": "SYNC_DRIFT", "source": "audio", "severity": "major", "confidence": 0.8,
                       "start": 0.0, "end": duration,
                       "description": f"Background bed in the dub is offset by {bg_offset:.0f} ms from the original.",
                       "fix_hint": "The whole dubbed mix is shifted; re-mux with the background aligned to video.",
                       "evidence": {"offset_ms": bg_offset}})
    t = _clock(timings, "detect", t)

    # ---- subtitles shipped inside the dub in the dub language (its own script)
    cues: list[dict] = []
    si = _subtitle_stream(dub)
    if si is not None:
        try:
            cues = parse_srt(extract_subtitles(dub.path, si, pair_dir / f"subs.s{si}.srt"))
            issues += _subtitle_issues(cues, d_raw, duration)
        except Exception as e:
            warnings.append(f"subtitle extraction failed: {e}")

    def script_in(s, e):
        return dub.lang.joiner.join(c["text"] for c in cues if c["end"] > s and c["start"] < e).strip()

    # ---- aligned lines + semantic judge
    rows = []
    for ln in lines:
        lo = min(ln.dub_start, ln.start) if ln.dub else ln.start
        hi = max(ln.dub_end, ln.end) if ln.dub else ln.end
        rows.append({"i": ln.index, "orig": speech.text_in(asr_o, ln.start, ln.end),
                     "dub": speech.text_in(asr_d, lo, hi), "script": script_in(lo, hi)})
    judged: dict[int, dict] = {}
    if cfg.llm_models:
        from .judge import judge

        judged = judge([r for r in rows if r["orig"] or r["dub"]], cfg.llm_models, src.lang.name, dub.lang.name,
                       pair_dir / "judge_cache.json", warnings)
        for ln in lines:
            for x in judged.get(ln.index, {}).get("issues", []):
                sev = x.get("severity") if x.get("severity") in SEV_WEIGHT else "minor"
                issues.append({"category": x["category"], "source": "semantic", "severity": sev,
                               "confidence": float(x.get("confidence") or 0.6),
                               "start": round(ln.start, 2), "end": round(max(ln.end, ln.dub_end or ln.end), 2),
                               "line_index": ln.index, "description": x.get("explanation", ""),
                               "fix_hint": x.get("fix_hint", "Re-translate this line."), "evidence": {}})
        t = _clock(timings, "judge", t)

    # ---- frames: video untouched? then mouth activity on flagged windows (language independent)
    if src.path.resolve() != dub.path.resolve():
        issues += visual.video_identity(src.path, dub.path, duration)
    if cfg.visual:
        try:
            ana = visual.MouthAnalyzer()
            cand = [x for x in issues if x["source"] == "timing" and x["category"] not in ("SYNC_DRIFT", "FAST_SPEECH")]
            cand.sort(key=lambda x: -SEV_WEIGHT[x["severity"]])
            for x in cand[: cfg.max_visual_windows]:
                s, e = x["start"], max(x["end"], x["start"] + 0.75)
                x["visual"] = ana.analyze(src.path, s, min(e, s + 4.0))
                visual.adjust(x, x["visual"])
        except Exception as e:
            warnings.append(f"visual analysis failed: {e}")
        t = _clock(timings, "visual", t)

    # ---- finalize
    issues = _dedupe(issues)
    issues.sort(key=lambda x: x["start"])
    by_line: dict[int, list[str]] = {}
    final: list[Issue] = []
    texts = {r["i"]: r for r in rows}
    for k, x in enumerate(issues):
        x["id"] = f"I{k + 1:03d}"
        li = x.pop("line_index", None)
        if li is not None:
            by_line.setdefault(li, []).append(x["id"])
            x.setdefault("original_text", texts[li]["orig"] or None)
            x.setdefault("dubbed_text", texts[li]["dub"] or None)
            x["evidence"]["line_index"] = li
        final.append(Issue(start_tc=tc(x["start"]), end_tc=tc(x["end"]), **x))

    aligned = []
    for ln in lines:
        j = judged.get(ln.index, {})
        aligned.append(AlignedLine(
            index=ln.index, orig_start=round(ln.start, 2), orig_end=round(ln.end, 2),
            dub_start=None if ln.dub_start is None else round(ln.dub_start, 2),
            dub_end=None if ln.dub_end is None else round(ln.dub_end, 2),
            onset_delta=None if ln.onset is None else round(ln.onset, 2), dub_coverage=round(ln.coverage, 2),
            original_text=texts[ln.index]["orig"], dubbed_text=texts[ln.index]["dub"],
            original_en=j.get("orig_en"), dubbed_en=j.get("dub_en"), issue_ids=by_line.get(ln.index, [])))

    st = timing.stats(lines, o_raw, d_raw, duration)
    counts: dict[str, int] = {}
    sevs = {"critical": 0, "major": 0, "minor": 0}
    penalty = 0.0
    for x in final:
        counts[x.category] = counts.get(x.category, 0) + 1
        sevs[x.severity] += 1
        penalty += SEV_WEIGHT[x.severity] * x.confidence
    score = round(100 * np.exp(-penalty / max(1.0, duration / 60 * 15)), 1)
    if any(x.source == "language" and x.severity == "critical" for x in final):
        score = 0.0  # whole track is in the wrong language: timing quality is meaningless
    summary = Summary(duration=round(duration, 2), background_offset_ms=bg_offset, issue_counts=counts,
                      severity_counts=sevs, score=score, **st)
    return Report(
        pair=f"{src.lang.tag}->{dub.lang.tag}", source=src.describe(), target=dub.describe(),
        inputs={"asr_engines": [asr_o["engine"], asr_d["engine"]], "subtitle_stream": si,
                "subtitle_cues": len(cues), "stems": stems},
        config={**{k: v for k, v in asdict(cfg).items() if k != "thresholds"}, "thresholds": asdict(tt)},
        summary=summary, issues=final, aligned_lines=aligned, stage_timings=timings, warnings=warnings)


def run(original: Path, dubs: list[Path], work: Path, cfg: Config) -> BatchReport:
    """One original, any number of dub files, each with any number of audio streams."""
    work.mkdir(parents=True, exist_ok=True)
    src = original_track(original, cfg, work)
    log.info("original %s audio %d: %s (%s)", original.name, src.index, src.lang.tag, src.lang_source)
    reports, skipped, failed = [], [], []
    for path in dubs:
        tracks, skip = dub_tracks(path, src, cfg, work)
        skipped += skip
        if not tracks:
            failed.append({"dub": str(path), "error": "no evaluable dub audio stream (see skipped_tracks)"})
        for d in tracks:
            try:
                reports.append(run_pair(src, d, work, cfg))
            except Exception as e:  # one broken pair must not sink the batch
                log.exception("pair %s failed", d.slug)
                failed.append({"dub": str(path), "audio_index": d.index, "error": f"{type(e).__name__}: {e}"})
    return BatchReport(original=src.describe(), reports=reports, skipped_tracks=skipped, failed=failed)
