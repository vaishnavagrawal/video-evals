"""Review contract (vreview.v1): what API clients, the UI and generation agents consume.

Invariant: `t0`/`t1` always come from deterministic layers (VAD, alignment, shot detection).
LLM/VLM judges classify evidence that is already located; they never produce timestamps.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "vreview.v1"

Severity = Literal["critical", "major", "minor"]
MediaRole = Literal["original", "candidate", "reference"]
RegenerateScope = Literal["shot", "segment", "full"]

# Triage vocabulary (rules live in vreview.triage).
Priority = Literal["blocker", "should-fix", "needs-human", "nit"]
Area = Literal["accuracy", "sync", "audio-mix", "delivery", "script", "language", "video", "other"]
Verdict = Literal["request_changes", "needs_human", "approve"]
TextSource = Literal["rule", "llm"]


class MediaRef(BaseModel):
    """One input file. `key` is the content-addressed blob key once ingested (media/<sha256><ext>)."""
    asset_id: str
    role: MediaRole
    key: str
    sha256: str
    filename: str | None = None


class Brief(BaseModel):
    """What the video was supposed to be. Evaluators decide applicability from it."""
    kind: Literal["dub", "generation"] = "dub"
    prompt: str | None = None
    script: str | None = None
    source_language: str | None = None                   # BCP-47 / ISO / name; None = detect
    target_languages: list[str] = Field(default_factory=list)
    options: dict = Field(default_factory=dict)          # evaluator-specific knobs, keyed by evaluator id


class Evidence(BaseModel):
    kind: Literal["frame", "clip", "audio", "text", "metric"]
    key: str | None = Field(default=None, description="Blob key for frame/clip/audio evidence")
    t: float | None = None
    data: dict = Field(default_factory=dict)


class Issue(BaseModel):
    id: str = Field(description="Stable within a review, e.g. 'dubbing:hi->es-419:I004'")
    evaluator: str
    category: str
    severity: Severity
    confidence: float
    t0: float
    t1: float
    track: str | None = Field(default=None, description="Sub-target, e.g. dub pair 'hi->es-419'")
    shot_id: str | None = None
    description: str
    fix_hint: str
    suggested_prompt_patch: str | None = None
    regenerate_scope: RegenerateScope = "segment"
    evidence: list[Evidence] = Field(default_factory=list)
    details: dict = Field(default_factory=dict)          # evaluator-specific payload (texts, metrics)

    # Set by vreview.triage; defaults keep reviews stored before triage existed parseable.
    priority: Priority | None = None
    tags: list[str] = Field(default_factory=list)
    area: Area | None = None
    thread_id: str | None = None


class EvaluatorResult(BaseModel):
    evaluator: str
    version: str
    status: Literal["ok", "skipped", "failed"]
    reason: str | None = None                            # why skipped/failed
    score: float | None = Field(default=None, description="0-100, 100 = no detected issues")
    issues: list[Issue] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict, description="name -> blob key")
    warnings: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)


class Thread(BaseModel):
    """One review comment: issues of a track that sit close together in time."""
    id: str = Field(description="'T001', 'T002', ... in time order within the review; not stable across runs")
    track: str | None = None
    t0: float
    t1: float
    priority: Priority = Field(description="Highest member priority: blocker > should-fix > needs-human > nit")
    areas: list[Area] = Field(default_factory=list, description="Primary issue's area first")
    issue_ids: list[str] = Field(default_factory=list, description="Time order")
    title: str
    comment: str = Field(description="Markdown: what is wrong, why it matters, how to fix")
    comment_source: TextSource = "rule"
    comment_model: str | None = Field(default=None, description="Model that wrote title/comment when comment_source is 'llm'")
    original_text: str | None = None
    dubbed_text: str | None = None


class Triage(BaseModel):
    """Code-review style view of a review. Deterministic; only the prose may come from an LLM.

    Every count here counts threads, not issues.
    """
    version: str = "1"
    verdict: Verdict
    counts: dict[str, int] = Field(default_factory=dict, description="Threads per priority; all four keys present")
    area_counts: dict[str, int] = Field(default_factory=dict, description="Threads per area (a thread counts once for each of its areas); largest first")
    threads: list[Thread] = Field(default_factory=list)
    summary: str = ""
    summary_source: TextSource = "rule"
    summary_model: str | None = None
    llm_warnings: list[str] = Field(default_factory=list)


class Review(BaseModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    brief: Brief
    media: list[MediaRef]
    results: list[EvaluatorResult]
    score: float | None = Field(default=None, description="Min over evaluator scores")
    created_at: datetime
    triage: Triage | None = None
