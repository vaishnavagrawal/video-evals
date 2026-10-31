"""Structured output contract. This is what the dubbing agent consumes."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "dubeval.v2"

Severity = Literal["critical", "major", "minor"]
Source = Literal["timing", "visual", "audio", "semantic", "video", "subtitle", "language"]
Category = Literal[
    # timing (original speech vs dubbed speech activity)
    "MISSING_DUB",          # original line has (almost) no dubbed speech
    "LATE_START",           # dub starts noticeably after the original line
    "EARLY_CUTOFF",         # dub ends while original actor is still speaking
    "MID_LINE_GAP",         # silence in the dub inside an original line
    "EARLY_START",          # dub starts before the original line
    "OVERRUN",              # dub keeps talking after the original line ended
    "UNMATCHED_SPEECH",     # dub speech where the original has no speech
    "SYNC_DRIFT",           # sustained onset offset over several lines
    "FAST_SPEECH",          # dub speaking rate far above its own median (cramming)
    # audio
    "BACKGROUND_DROPOUT",   # music/effects bed missing or much quieter in the dub
    "ENERGY_MISMATCH",      # loud/emotional original line delivered flat (or vice versa)
    # semantic (LLM judge)
    "MEANING_MISMATCH",
    "MISSING_CONTENT",
    "ADDED_CONTENT",
    "TONE_MISMATCH",
    "NAME_OR_TERM_ERROR",
    "UNTRANSLATED",
    "SCRIPT_DEVIATION",     # spoken dub differs from the dub's own subtitle script
    "WRONG_LANGUAGE",       # dub track's spoken language differs from the declared target
    # subtitle / container / video
    "SUBTITLE_AUDIO_DESYNC",
    "VIDEO_MISMATCH",
    "DURATION_MISMATCH",
]


class VisualEvidence(BaseModel):
    frames_analyzed: int
    face_frames: int
    max_face_area: float = Field(description="Largest face box as a fraction of the frame")
    mouth_activity: float = Field(description="Std-dev of mouth opening ratio on the most active face")
    speaking_likely: bool | None = Field(description="None when no usable face was found")


class Issue(BaseModel):
    id: str
    category: Category
    source: Source
    severity: Severity
    confidence: float
    start: float
    end: float
    start_tc: str
    end_tc: str
    description: str
    original_text: str | None = None
    dubbed_text: str | None = None
    fix_hint: str
    evidence: dict = Field(default_factory=dict)
    visual: VisualEvidence | None = None


class AlignedLine(BaseModel):
    """One original utterance and what the dub put against it, for the regenerating agent."""
    index: int
    orig_start: float
    orig_end: float
    dub_start: float | None
    dub_end: float | None
    onset_delta: float | None
    dub_coverage: float
    original_text: str
    dubbed_text: str
    original_en: str | None = Field(default=None, description="LLM English rendering of the original line")
    dubbed_en: str | None = Field(default=None, description="LLM English back-translation of the dub")
    issue_ids: list[str] = Field(default_factory=list)


class Summary(BaseModel):
    duration: float
    orig_speech_seconds: float
    dub_speech_seconds: float
    speech_overlap_ratio: float = Field(description="Fraction of original speech time also covered by dubbed speech")
    unmatched_dub_ratio: float = Field(description="Fraction of dubbed speech time outside original speech")
    median_onset_delta: float | None
    p90_abs_onset_delta: float | None
    background_offset_ms: float | None
    issue_counts: dict[str, int]
    severity_counts: dict[str, int]
    score: float = Field(description="0-100 heuristic quality score (100 = no detected issues)")


class Report(BaseModel):
    """One (original track, dub track) language pair."""
    schema_version: str = SCHEMA_VERSION
    pair: str = Field(description="'<src tag>-><tgt tag>', e.g. 'hi->es-419'")
    source: dict = Field(description="Original track: file, stream, container tag, detected/declared language")
    target: dict = Field(description="Dub track: file, stream, container tag, detected/declared language")
    inputs: dict
    config: dict
    summary: Summary
    issues: list[Issue]
    aligned_lines: list[AlignedLine]
    stage_timings: dict[str, float]
    warnings: list[str] = Field(default_factory=list)


class BatchReport(BaseModel):
    """All dub tracks evaluated against one original (m sources x n targets = one batch per original)."""
    schema_version: str = SCHEMA_VERSION
    original: dict
    reports: list[Report]
    skipped_tracks: list[dict] = Field(default_factory=list,
                                       description="Audio streams not evaluated, with the reason")
    failed: list[dict] = Field(default_factory=list, description="Pairs that crashed: {dub, error}")
