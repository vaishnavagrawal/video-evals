"""Audio-video judge: a multimodal model watches and listens to short windows of the media.

Generic second-opinion reviewer. It is not tied to dubbing or to a vendor:
- Subjects are (candidate stream, optional reference stream) pairs. Dubbing gives (dub, original); a
  generation brief gives the candidate alone.
- Windows come from issues that deterministic layers already located (padded, merged, capped). With no
  issues it samples evenly spaced windows. The model never invents timestamps: confirmations attach to
  existing issues and new findings inherit the window bounds (see vreview.schema invariant).
- Models are a fallback list of `provider:model` references (dubeval.providers). How video reaches the
  model follows the provider's `video` mode: `video_url` (clip with audio as a data URL), `frames`
  (JPEG frames, no audio) or `none` (skipped).

Output: an `av_judge` EvaluatorResult with new findings, plus `reviews`, a map issue id -> verdict that the
workflow folds into the other evaluators' issues (`apply_reviews`).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from vreview.schema import Brief, EvaluatorResult, Issue, MediaRef
from vreview.storage.base import BlobStore

log = logging.getLogger("vreview.av_judge")

EVALUATOR_ID = "av_judge"
VERSION = "1"
PROMPT_VERSION = "1"

CATEGORIES = {
    "LIP_SYNC": "mouth movement on screen does not match the speech timing",
    "VOICE_MISMATCH": "voice does not fit the on-screen speaker (gender, age, identity) or changes between lines",
    "EMOTION_MISMATCH": "delivery/emotion differs from the reference or from what the scene shows",
    "AUDIO_ARTIFACT": "clicks, robotic/TTS artifacts, clipping, dropouts, bad mix of voice vs background",
    "VISUAL_ARTIFACT": "glitches, morphing, flicker, broken hands/faces/text",
    "BRIEF_MISMATCH": "content contradicts the brief, prompt or script",
}
_SEV_RANK = {"critical": 0, "major": 1, "minor": 2}


class AVJudgeConfig(BaseModel):
    models: list[str] = Field(default_factory=list)
    max_windows: int = 10            # per subject
    pad_s: float = 1.0
    min_window_s: float = 4.0
    max_window_s: float = 12.0
    sample_window_s: float = 8.0     # evenly spaced windows when there is nothing to check
    width: int = 480
    frames_per_window: int = 6       # `frames` providers
    workers: int = 3
    timeout_s: float = 180.0
    min_confidence: float = 0.5      # new findings below this are dropped
    dispute_confidence: float = 0.3  # disputed issues are capped here, below triage min_confidence (0.4) -> needs-human

    @classmethod
    def from_brief(cls, brief: Brief, models: list[str]) -> "AVJudgeConfig":
        opts = dict(brief.options.get(EVALUATOR_ID, {}))
        opts.setdefault("models", list(models))
        return cls(**opts)


class Stream(BaseModel):
    media: MediaRef
    audio_index: int | None = None   # ffmpeg 0:a:N; None = first audio stream if any
    label: str = ""                  # e.g. "es dub", "original (hi)"


class Subject(BaseModel):
    track: str | None = None         # issue.track this subject covers (e.g. "hi->es"); None = whole review
    candidate: Stream
    reference: Stream | None = None


@dataclass
class Window:
    t0: float
    t1: float
    issues: list[Issue] = field(default_factory=list)


# ------------------------------------------------------------------------------------------- windows

def plan_windows(issues: list[Issue], duration: float, cfg: AVJudgeConfig) -> list[Window]:
    """Most severe issues first, padded to >= min_window_s, merged while <= max_window_s, capped."""
    if duration <= 0:
        return []
    if not issues:
        n = max(1, min(cfg.max_windows, int(duration // cfg.sample_window_s) or 1))
        step = duration / n
        return [Window(round(k * step, 2), round(min(duration, k * step + min(step, cfg.sample_window_s)), 2))
                for k in range(n)]
    ranked = sorted(issues, key=lambda i: (_SEV_RANK.get(i.severity, 3), -i.confidence, i.t0))
    windows: list[Window] = []
    for iss in ranked:
        lo, hi = min(iss.t0, iss.t1), max(iss.t0, iss.t1)
        if hi - lo > cfg.max_window_s:            # long issue: look at its start
            hi = lo + cfg.max_window_s
        mid = (lo + hi) / 2
        span = max(hi - lo + 2 * cfg.pad_s, cfg.min_window_s)
        lo, hi = max(0.0, mid - span / 2), min(duration, mid + span / 2)
        for w in windows:
            if lo <= w.t1 and hi >= w.t0 and max(hi, w.t1) - min(lo, w.t0) <= cfg.max_window_s:
                w.t0, w.t1 = min(lo, w.t0), max(hi, w.t1)
                w.issues.append(iss)
                break
        else:
            if len(windows) < cfg.max_windows:
                windows.append(Window(lo, hi, [iss]))
    for w in windows:
        w.t0, w.t1 = round(w.t0, 2), round(w.t1, 2)
        w.issues.sort(key=lambda i: i.t0)
    return sorted(windows, key=lambda w: w.t0)


# ---------------------------------------------------------------------------------------------- media

def _run(cmd: list[str]) -> bytes:
    return subprocess.run(cmd, check=True, capture_output=True).stdout


def duration_of(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


def cut_clip(src: Path, stream: Stream, t0: float, t1: float, dst: Path, width: int) -> Path:
    """Small H.264/AAC clip of one window with the chosen audio stream (keeps request size low)."""
    if not dst.exists():
        audio = f"0:a:{stream.audio_index}?" if stream.audio_index is not None else "0:a:0?"
        _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t0:.3f}", "-i", str(src), "-t", f"{max(0.5, t1 - t0):.3f}",
              "-map", "0:v:0", "-map", audio, "-vf", f"scale={width}:-2", "-c:v", "libx264", "-preset", "veryfast",
              "-crf", "30", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(dst)])
    return dst


def frames_of(src: Path, t0: float, t1: float, n: int, width: int) -> list[bytes]:
    out = []
    for k in range(n):
        t = t0 + (t1 - t0) * (k + 0.5) / n
        out.append(_run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1",
                         "-vf", f"scale={width}:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "5", "-"]))
    return out


def _media_part(mode: str, path: Path, t0: float, t1: float, cfg: AVJudgeConfig) -> list[dict]:
    if mode == "video_url":
        b64 = base64.b64encode(path.read_bytes()).decode()
        return [{"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}}]
    return [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(f).decode()}}
            for f in frames_of(path, 0.0, t1 - t0, cfg.frames_per_window, cfg.width)]


# --------------------------------------------------------------------------------------------- prompt

PROMPT = """You are a senior video QA reviewer. You watch and listen to one short window of a video and
judge what a human viewer would notice. Be strict but fair: report only problems you can actually see or
hear in this window. Times are seconds relative to the window start.

Context:
{context}

Window: {t0:.2f}s to {t1:.2f}s of the full video ({span:.1f}s long).
{media_note}

Automated checks already flagged these issues in this window (they may be false alarms):
{issues}

Tasks:
1. For EVERY flagged issue, decide from what you see and hear: "confirm" (a viewer would notice it),
   "dispute" (not actually a problem here) or "unsure". Give a one-sentence reason.
2. Report up to 3 additional problems the checks missed, only from these categories:
{categories}
   Severity: critical = breaks the scene; major = clearly noticeable; minor = subtle.

Return JSON only:
{{"reviews":[{{"issue_id":"...","verdict":"confirm|dispute|unsure","confidence":0.0-1.0,"reason":"..."}}],
 "findings":[{{"category":"...","severity":"critical|major|minor","confidence":0.0-1.0,
   "explanation":"one sentence","fix_hint":"concrete instruction for the generator/dubbing system"}}],
 "observation":"one sentence on overall quality of this window"}}"""


def _context(brief: Brief, subject: Subject) -> str:
    lines = [f"- brief kind: {brief.kind}"]
    if brief.source_language:
        lines.append(f"- source language: {brief.source_language}")
    if brief.target_languages:
        lines.append(f"- target language(s): {', '.join(brief.target_languages)}")
    if subject.track:
        lines.append(f"- track under review: {subject.track}")
    if brief.prompt:
        lines.append(f"- prompt: {brief.prompt[:600]}")
    if brief.script:
        lines.append(f"- script (excerpt): {brief.script[:600]}")
    return "\n".join(lines)


def _issue_rows(w: Window) -> str:
    if not w.issues:
        return "(none)"
    return json.dumps([{"issue_id": i.id, "category": i.category, "severity": i.severity,
                        "t0": round(max(0.0, i.t0 - w.t0), 2), "t1": round(max(0.0, i.t1 - w.t0), 2),
                        "description": (i.description or "")[:300]} for i in w.issues], ensure_ascii=False)


def build_messages(brief: Brief, subject: Subject, w: Window, mode: str, cand: Path, ref: Path | None,
                   cfg: AVJudgeConfig) -> list[dict]:
    if mode == "video_url":
        note = "Each clip is video with its audio track."
    else:
        note = "You get still frames only (no audio); do not judge sound."
    if ref is not None:
        note += (f" Clip A is the reference ({subject.reference.label or 'original'}); clip B is the candidate "
                 f"under review ({subject.candidate.label or 'candidate'}). Judge clip B; use A only as reference.")
    text = PROMPT.format(context=_context(brief, subject), t0=w.t0, t1=w.t1, span=w.t1 - w.t0, media_note=note,
                         issues=_issue_rows(w),
                         categories="\n".join(f"   - {k}: {v}" for k, v in CATEGORIES.items()))
    content: list[dict] = [{"type": "text", "text": text}]
    if ref is not None:
        content += [{"type": "text", "text": "Clip A (reference):"}, *_media_part(mode, ref, w.t0, w.t1, cfg),
                    {"type": "text", "text": "Clip B (candidate):"}]
    content += _media_part(mode, cand, w.t0, w.t1, cfg)
    return [{"role": "user", "content": content}]


def _parse(text: str, issue_ids: set[str]) -> dict:
    text = (text or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    reviews = []
    for r in data.get("reviews") or []:
        if isinstance(r, dict) and r.get("issue_id") in issue_ids and r.get("verdict") in ("confirm", "dispute", "unsure"):
            reviews.append({"issue_id": r["issue_id"], "verdict": r["verdict"],
                            "confidence": _conf(r.get("confidence")), "reason": str(r.get("reason") or "")[:400]})
    findings = []
    for f in data.get("findings") or []:
        if isinstance(f, dict) and str(f.get("category", "")).upper() in CATEGORIES:
            sev = str(f.get("severity", "")).lower()
            findings.append({"category": str(f["category"]).upper(), "severity": sev if sev in _SEV_RANK else "minor",
                             "confidence": _conf(f.get("confidence")),
                             "explanation": str(f.get("explanation") or "")[:400],
                             "fix_hint": str(f.get("fix_hint") or "Review and regenerate this segment.")[:400]})
    return {"reviews": reviews, "findings": findings[:3], "observation": str(data.get("observation") or "")[:400]}


def _conf(x: Any) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.5


# ------------------------------------------------------------------------------------------------ run

class AVJudgeOutput(BaseModel):
    result: EvaluatorResult
    reviews: dict[str, dict] = Field(default_factory=dict)   # issue id -> {verdict, confidence, reason, model}


def _ask(client: Any, models: list[str], messages: list[dict], ids: set[str], timeout: float) -> tuple[dict | None, str | None, str | None]:
    last = None
    for model in models:
        for attempt in range(2):
            try:
                r = client.chat.completions.create(model=model, messages=messages, timeout=timeout,
                                                   response_format={"type": "json_object"})
                return _parse(r.choices[0].message.content or "", ids), model, None
            except Exception as e:   # provider missing / timeout / 429 / bad JSON -> retry, then next model
                last = f"{model}: {type(e).__name__}: {' '.join(str(e).split())[:160]}"
                log.warning("av_judge attempt failed (%s)", last)
                if attempt == 0:
                    time.sleep(2)
    return None, None, last


def _video_mode(models: list[str]) -> str:
    """Mode of the first model whose provider is configured; the cut clips are shared by all models."""
    from dubeval.providers import ProviderError, parse, provider

    for m in models:
        try:
            return provider(parse(m).provider).video
        except ProviderError:
            continue
    return "none"


def run(store: BlobStore, brief: Brief, subjects: list[Subject], issues: list[Issue], cfg: AVJudgeConfig,
        scratch: Path, client: Any | None = None, progress=None) -> AVJudgeOutput:
    """Judge every subject's windows. Never raises for model failures (they become warnings).

    `progress(done, total, detail)` is called after each window (for pipeline events).
    """
    t_start = time.time()
    res = EvaluatorResult(evaluator=EVALUATOR_ID, version=VERSION, status="ok")
    out = AVJudgeOutput(result=res)
    if not cfg.models:
        res.status, res.reason = "skipped", "no audio-video judge models configured (VREVIEW_AV_JUDGE_MODELS)"
        return out
    mode = _video_mode(cfg.models)
    if mode == "none":
        res.status, res.reason = "skipped", "no configured provider for the av_judge models accepts video or images"
        return out
    if client is None:
        from dubeval.providers import RoutedClient
        client = RoutedClient(timeout=cfg.timeout_s, max_retries=0)

    scratch.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[Subject, Window, Path, Path | None]] = []
    for s_i, subj in enumerate(subjects):
        cand_src = store.get_file(subj.candidate.media.key, scratch / f"{subj.candidate.media.sha256}{Path(subj.candidate.media.key).suffix or '.mp4'}")
        ref_src = (store.get_file(subj.reference.media.key, scratch / f"{subj.reference.media.sha256}{Path(subj.reference.media.key).suffix or '.mp4'}")
                   if subj.reference else None)
        dur = duration_of(cand_src)
        mine = [i for i in issues if subj.track is None or i.track == subj.track]
        for w_i, w in enumerate(plan_windows(mine, dur, cfg)):
            c = cut_clip(cand_src, subj.candidate, w.t0, w.t1, scratch / f"s{s_i}_w{w_i}_cand.mp4", cfg.width)
            r = (cut_clip(ref_src, subj.reference, w.t0, w.t1, scratch / f"s{s_i}_w{w_i}_ref.mp4", cfg.width)
                 if ref_src is not None and subj.reference else None)
            jobs.append((subj, w, c, r))

    done = 0
    used_models: set[str] = set()

    def judge(job):
        nonlocal done
        subj, w, cand, ref = job
        ids = {i.id for i in w.issues}
        h = hashlib.sha256()
        for p in (cand, ref):
            if p is not None:
                h.update(p.read_bytes())
        h.update(json.dumps({"p": PROMPT_VERSION, "m": cfg.models, "mode": mode, "issues": _issue_rows(w),
                             "ctx": _context(brief, subj)}, sort_keys=True).encode())
        key = f"cache/{EVALUATOR_ID}/{h.hexdigest()}.json"
        if store.exists(key):
            cached = json.loads(store.get_bytes(key))
            parsed, model, err = cached["parsed"], cached["model"], None
        else:
            messages = build_messages(brief, subj, w, mode, cand, ref, cfg)
            parsed, model, err = _ask(client, cfg.models, messages, ids, cfg.timeout_s)
            if parsed is not None:
                store.put_bytes(key, json.dumps({"parsed": parsed, "model": model}).encode(), "application/json")
        done += 1
        if progress:
            try:
                progress(done, len(jobs), {"model": model})
            except Exception:
                pass
        return subj, w, parsed, model, err

    n_new = 0
    windows_detail = []
    with ThreadPoolExecutor(max(1, cfg.workers)) as ex:
        for subj, w, parsed, model, err in ex.map(judge, jobs):
            if parsed is None:
                res.warnings.append(f"av_judge failed for {subj.track or 'video'} {w.t0:.1f}-{w.t1:.1f}s ({err})")
                windows_detail.append({"track": subj.track, "t0": w.t0, "t1": w.t1, "status": "failed"})
                continue
            used_models.add(model)
            for rv in parsed["reviews"]:
                out.reviews[rv["issue_id"]] = {**rv, "model": model, "window": [w.t0, w.t1]}
            for f in parsed["findings"]:
                if f["confidence"] < cfg.min_confidence:
                    continue
                n_new += 1
                res.issues.append(Issue(
                    id=f"{EVALUATOR_ID}:{subj.track or 'video'}:A{n_new:03d}", evaluator=EVALUATOR_ID,
                    category=f["category"], severity=f["severity"], confidence=f["confidence"],
                    t0=w.t0, t1=w.t1, track=subj.track, description=f["explanation"], fix_hint=f["fix_hint"],
                    details={"source": EVALUATOR_ID, "model": model, "observation": parsed["observation"]}))
            windows_detail.append({"track": subj.track, "t0": w.t0, "t1": w.t1, "status": "ok", "model": model,
                                   "reviewed": len(parsed["reviews"]), "findings": len(parsed["findings"]),
                                   "observation": parsed["observation"]})

    verdicts = [r["verdict"] for r in out.reviews.values()]
    res.summary = {"windows": len(jobs), "windows_failed": sum(1 for w in windows_detail if w["status"] == "failed"),
                   "video_mode": mode, "models": sorted(used_models), "confirmed": verdicts.count("confirm"),
                   "disputed": verdicts.count("dispute"), "unsure": verdicts.count("unsure"),
                   "new_findings": len(res.issues), "window_details": windows_detail}
    res.incomplete = [w for w in res.warnings if w.startswith("av_judge failed")]
    if jobs and res.summary["windows_failed"] == len(jobs):
        res.status, res.reason = "failed", "every audio-video judge call failed"
    res.timings = {"av_judge": round(time.time() - t_start, 2)}
    return out


def apply_reviews(results: list[EvaluatorResult], reviews: dict[str, dict], cfg: AVJudgeConfig) -> None:
    """Fold verdicts into the reviewed issues. Disputed issues keep their timestamps but drop to low
    confidence, so triage routes them to needs-human instead of blocking."""
    for r in results:
        for iss in r.issues:
            rv = reviews.get(iss.id)
            if not rv:
                continue
            iss.details = {**(iss.details or {}), "av_review": rv}
            if rv["verdict"] == "dispute" and rv["confidence"] >= 0.6:
                iss.confidence = min(iss.confidence, cfg.dispute_confidence)
            elif rv["verdict"] == "confirm":
                iss.confidence = max(iss.confidence, min(0.95, rv["confidence"]))
