"""Typed I/O between dubbing stages. These cross Hatchet task boundaries, so everything is JSON-able
and large artefacts are referenced by blob key, never inlined."""
from __future__ import annotations

from pydantic import BaseModel, Field

from ...schema import Brief, Issue, MediaRef


class DubConfig(BaseModel):
    src_lang: str | None = None
    tgt_langs: list[str] = Field(default_factory=list)
    orig_audio: int | None = None
    dub_audio: int | None = None
    asr_cloud_model: str | None = "groq/whisper-large-v3"
    asr_local_model: str = "small"
    llm_models: list[str] = Field(default_factory=lambda: ["gemini-flash", "auto/best-fast"])
    judge_mode: str = "fallback"            # "fallback" | "ensemble" (every model judges, answers merged)
    separate: bool = False
    visual: bool = True
    max_visual_windows: int = 60
    threads: int = 8

    @classmethod
    def from_brief(cls, brief: Brief) -> "DubConfig":
        """Brief options win; then the deployment's model roles (VREVIEW_JUDGE_MODELS, VREVIEW_ASR_MODEL)."""
        from vreview.config import settings

        s = settings()
        defaults: dict = {}
        if s.judge_models:
            defaults["llm_models"] = list(s.judge_models)
        if s.judge_mode:
            defaults["judge_mode"] = s.judge_mode
        if s.asr_model:
            defaults["asr_cloud_model"] = None if s.asr_model.strip().lower() == "none" else s.asr_model.strip()
        return cls(src_lang=brief.source_language, tgt_langs=brief.target_languages,
                   **{**defaults, **brief.options.get("dubbing", {})})


class TrackRef(BaseModel):
    """One audio stream of one ingested media file."""
    media: MediaRef
    audio_index: int
    role: str                               # "original" | "dub"
    slug: str                               # "<sha256[:16]>.a<audio_index>", stable across runs
    lang_tag: str | None = None             # resolved BCP-47 tag
    lang_source: str = "detected"           # "declared" | "detected" | "tag"
    lid: dict = Field(default_factory=dict)
    stream: dict = Field(default_factory=dict)
    wav_key: str                            # blob key of the 16 kHz mono wav


class TrackPlan(BaseModel):
    original: TrackRef
    dubs: list[TrackRef]
    skipped: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TrackFeatures(BaseModel):
    """Per-track heavy outputs (stems, ASR). Cached by content hash, shared across pairs and runs."""
    track: TrackRef
    asr_key: str
    vocals_key: str | None = None           # None when separation is off or failed
    bed_key: str | None = None
    warnings: list[str] = Field(default_factory=list)


class PairResult(BaseModel):
    pair: str                               # "<src lang>-><dub lang>" (+ dub slug if ambiguous)
    report_key: str                         # full dubeval.v2 Report JSON in blob storage
    score: float
    issues: list[Issue]
    summary: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
