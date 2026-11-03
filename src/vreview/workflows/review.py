"""Hatchet workflows for video review (vreview).

Defines:
- `track_features_workflow`: child workflow ("dubbing-track-features") extracting ASR/stems per track
- `pair_workflow`: child workflow ("dubbing-pair") executing pairwise dubbing evaluation
- `review_workflow`: root workflow ("review") managing ingest -> plan -> features -> pairs -> assemble -> finalize
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hatchet_sdk import Context
from hatchet_sdk.types.concurrency import ConcurrencyExpression, ConcurrencyLimitStrategy
from pydantic import BaseModel, Field

from vreview.config import settings
from vreview.evaluators.dubbing.models import (
    DubConfig,
    PairResult,
    TrackFeatures,
    TrackPlan,
    TrackRef,
)
from vreview.hatchet import hatchet
from vreview.jobs import ReviewInput
from vreview.schema import EvaluatorResult, MediaRef, Review
from vreview.storage import get_store
from vreview.storage.base import BlobStore, review_key
from vreview.triage import triage


def _stage(run_id: str, stage: str) -> None:
    """Mirror the current DAG stage onto the run row so failures report where they happened."""
    from vreview.db import repo

    repo.mark_run(run_id, "running", stage=stage)


logger = logging.getLogger(__name__)

# How long finalize waits for the LLM to write thread comments before it ships the rule-based text.
# Kept well under finalize's execution timeout so a slow gateway can't fail the whole review.
TRIAGE_LLM_TIMEOUT_S = 600.0


# ---------------------------------------------------------------------------
# Workflow input / output models (all must be dict, BaseModel, or dataclass)
# ---------------------------------------------------------------------------

class TrackFeaturesInput(BaseModel):
    run_id: str
    track: TrackRef
    cfg: DubConfig


class PairInput(BaseModel):
    run_id: str
    src: TrackFeatures
    dub: TrackFeatures
    cfg: DubConfig


class IngestOutput(BaseModel):
    media: list[MediaRef] = Field(default_factory=list)


class DubbingPlanOutput(BaseModel):
    plan: TrackPlan | None = None
    cfg: DubConfig | None = None
    skipped_result: EvaluatorResult | None = None


class DubbingPairsOutput(BaseModel):
    pairs: list[PairResult] = Field(default_factory=list)


class AssembleOutput(BaseModel):
    results: list[EvaluatorResult] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Child workflow: dubbing-track-features
# ---------------------------------------------------------------------------

track_features_workflow = hatchet.workflow(
    name="dubbing-track-features",
    input_validator=TrackFeaturesInput,
    concurrency=ConcurrencyExpression.from_int(4),  # global cap on features
)


@track_features_workflow.task(
    name="track_features",
    execution_timeout=timedelta(minutes=60),
    retries=2,
)
def run_track_features(input: TrackFeaturesInput, ctx: Context) -> TrackFeatures:
    """Sync heavy task: ASR + optional stems for one audio track."""
    from vreview.evaluators.dubbing.stages import track_features

    scratch = settings().scratch_dir / input.run_id / f"feat_{input.track.slug}"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        store = get_store()
        return track_features(store, input.track, input.cfg, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# Child workflow: dubbing-pair
# ---------------------------------------------------------------------------

pair_workflow = hatchet.workflow(
    name="dubbing-pair",
    input_validator=PairInput,
    concurrency=ConcurrencyExpression(
        expression="has(additional_metadata.tenant) ? additional_metadata.tenant : 'default'",
        max_runs=4,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)


@pair_workflow.task(
    name="run_pair",
    execution_timeout=timedelta(minutes=30),
    retries=2,
)
def run_dubbing_pair(input: PairInput, ctx: Context) -> PairResult:
    """Sync heavy task: evaluates one original-dub pair."""
    from vreview.evaluators.dubbing.stages import run_pair

    scratch = settings().scratch_dir / input.run_id / f"pair_{input.dub.track.slug}"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        store = get_store()
        return run_pair(store, input.run_id, input.src, input.dub, input.cfg, scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main review workflow: review
# ---------------------------------------------------------------------------

review_workflow = hatchet.workflow(
    name="review",
    input_validator=ReviewInput,
    concurrency=ConcurrencyExpression(
        expression="has(additional_metadata.tenant) ? additional_metadata.tenant : 'default'",
        max_runs=4,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)


@review_workflow.task(
    name="ingest",
    execution_timeout=timedelta(minutes=15),
    retries=3,
)
def ingest_task(input: ReviewInput, ctx: Context) -> IngestOutput:
    """Ingest media assets into blob storage and mark run as running."""
    from vreview.db import repo
    from vreview.ingest import ingest

    _stage(input.run_id, "ingest")

    store = get_store()
    scratch = settings().scratch_dir / input.run_id / "ingest"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        media_refs: list[MediaRef] = []
        for item in input.media:
            ref = ingest(store, item, scratch)
            media_refs.append(ref)
        return IngestOutput(media=media_refs)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@review_workflow.task(
    name="dubbing_plan",
    parents=[ingest_task],
    execution_timeout=timedelta(minutes=5),
    retries=1,
)
def dubbing_plan_task(input: ReviewInput, ctx: Context) -> DubbingPlanOutput:
    """Plans dubbing tracks or skips cleanly if not applicable or not requested."""
    _stage(input.run_id, "dubbing_plan")
    ingest_out: IngestOutput = ctx.task_output(ingest_task)
    media_refs = ingest_out.media

    # If an explicit list of evaluators was provided and dubbing was omitted, skip cleanly
    if input.evaluators is not None and "dubbing" not in input.evaluators:
        return DubbingPlanOutput(
            skipped_result=EvaluatorResult(
                evaluator="dubbing",
                version="1",
                status="skipped",
                reason="Evaluator 'dubbing' not requested",
            )
        )

    from vreview.evaluators.dubbing.stages import DubbingEvaluator, plan_tracks

    evaluator = DubbingEvaluator()
    skip_reason = evaluator.applies_to(input.brief, media_refs)
    if skip_reason is not None:
        return DubbingPlanOutput(
            skipped_result=EvaluatorResult(
                evaluator="dubbing",
                version="1",
                status="skipped",
                reason=skip_reason,
            )
        )

    cfg = DubConfig.from_brief(input.brief)
    scratch = settings().scratch_dir / input.run_id / "plan"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        store = get_store()
        plan = plan_tracks(store, media_refs, cfg, scratch)
        return DubbingPlanOutput(plan=plan, cfg=cfg)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@review_workflow.task(
    name="dubbing_features",
    parents=[dubbing_plan_task],
    execution_timeout=timedelta(minutes=65),
)
async def dubbing_features_task(input: ReviewInput, ctx: Context) -> dict[str, TrackFeatures]:
    """Fans out feature extraction to child workflow dubbing-track-features per unique track."""
    await asyncio.to_thread(_stage, input.run_id, "dubbing_features")
    plan_out: DubbingPlanOutput = ctx.task_output(dubbing_plan_task)
    if plan_out.skipped_result is not None or plan_out.plan is None:
        return {}

    plan = plan_out.plan
    cfg = plan_out.cfg or DubConfig.from_brief(input.brief)

    # Collect unique tracks: original + all candidate dubs
    track_map: dict[str, TrackRef] = {plan.original.slug: plan.original}
    for dub in plan.dubs:
        track_map[dub.slug] = dub

    if not track_map:
        return {}

    tenant = str(ctx.additional_metadata.get("tenant", "default"))

    # Fan out to child workflow `dubbing-track-features` in parallel
    items = [
        track_features_workflow.create_bulk_run_item(
            input=TrackFeaturesInput(run_id=input.run_id, track=track, cfg=cfg),
            child_key=f"{input.run_id}:{track.slug}",
            additional_metadata={"tenant": tenant, "run_id": input.run_id},
        )
        for track in track_map.values()
    ]

    results = await track_features_workflow.aio_run_many(items, wait_for_result=True)
    features_by_slug: dict[str, TrackFeatures] = {}
    for res in results:
        if isinstance(res, BaseException):
            raise res
        if isinstance(res, dict):
            raw = res.get("track_features") or res.get("run_track_features")
            if raw is None and res:
                raw = next(iter(res.values()))
            if raw:
                feat = TrackFeatures.model_validate(raw)
                features_by_slug[feat.track.slug] = feat

    return features_by_slug


@review_workflow.task(
    name="dubbing_pairs",
    parents=[dubbing_plan_task, dubbing_features_task],
    execution_timeout=timedelta(minutes=35),
)
async def dubbing_pairs_task(input: ReviewInput, ctx: Context) -> DubbingPairsOutput:
    """Fans out pair evaluation to child workflow dubbing-pair per dub candidate."""
    await asyncio.to_thread(_stage, input.run_id, "dubbing_pairs")
    plan_out: DubbingPlanOutput = ctx.task_output(dubbing_plan_task)
    if plan_out.skipped_result is not None or plan_out.plan is None:
        return DubbingPairsOutput(pairs=[])

    plan = plan_out.plan
    if not plan.dubs:
        return DubbingPairsOutput(pairs=[])

    features_by_slug: dict[str, TrackFeatures] = ctx.task_output(dubbing_features_task)
    src_feat = features_by_slug.get(plan.original.slug)
    if not src_feat:
        raise RuntimeError(f"Missing features for original track {plan.original.slug}")

    cfg = plan_out.cfg or DubConfig.from_brief(input.brief)
    tenant = str(ctx.additional_metadata.get("tenant", "default"))

    items = [
        pair_workflow.create_bulk_run_item(
            input=PairInput(
                run_id=input.run_id,
                src=src_feat,
                dub=features_by_slug[dub.slug],
                cfg=cfg,
            ),
            child_key=f"{input.run_id}:{dub.slug}",
            additional_metadata={"tenant": tenant, "run_id": input.run_id},
        )
        for dub in plan.dubs
        if dub.slug in features_by_slug
    ]

    if not items:
        return DubbingPairsOutput(pairs=[])

    results = await pair_workflow.aio_run_many(items, wait_for_result=True)
    pair_results: list[PairResult] = []
    for res in results:
        if isinstance(res, BaseException):
            raise res
        if isinstance(res, dict):
            raw = res.get("run_pair") or res.get("pair")
            if raw is None and res:
                raw = next(iter(res.values()))
            if raw:
                pair_results.append(PairResult.model_validate(raw))

    return DubbingPairsOutput(pairs=pair_results)


@review_workflow.task(
    name="assemble",
    parents=[dubbing_plan_task, dubbing_pairs_task],
    execution_timeout=timedelta(minutes=5),
    retries=1,
)
def assemble_task(input: ReviewInput, ctx: Context) -> AssembleOutput:
    """Assembles pair evaluation results into an EvaluatorResult list."""
    _stage(input.run_id, "assemble")
    plan_out: DubbingPlanOutput = ctx.task_output(dubbing_plan_task)
    if plan_out.skipped_result is not None:
        return AssembleOutput(results=[plan_out.skipped_result])

    pairs_out: DubbingPairsOutput = ctx.task_output(dubbing_pairs_task)
    pair_results = pairs_out.pairs
    from vreview.evaluators.dubbing.stages import assemble

    assert plan_out.plan is not None
    eval_res = assemble(pair_results, plan_out.plan)
    return AssembleOutput(results=[eval_res])


def _triage_langs(review: Review) -> tuple[str | None, str | None]:
    """Source and target language for the LLM prompt: the brief's if declared, else the single dub pair's."""
    src = review.brief.source_language
    targets = review.brief.target_languages
    tgt = targets[0] if len(targets) == 1 else None
    if src is None or tgt is None:
        tracks = {i.track for r in review.results for i in r.issues if i.track}
        if len(tracks) == 1 and "->" in (track := next(iter(tracks))):
            pair_src, pair_tgt = track.split("->", 1)
            src, tgt = src or pair_src, tgt or pair_tgt
    return src, tgt


def _merge_llm_text(tri, llm) -> None:
    """Overlay LLM titles/comments/summary on the rule text; whatever the LLM did not return stays rule-based."""
    llm = llm or {}
    comments = llm.get("threads") or {}
    for thread in tri.threads:
        entry = comments.get(thread.id)
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        comment = str(entry.get("comment") or "").strip()
        if not (title or comment):
            continue
        if title:
            thread.title = title
        if comment:
            thread.comment = comment
        thread.comment_source = "llm"
        thread.comment_model = str(entry["model"]) if entry.get("model") else None
    summary = str(llm.get("summary") or "").strip()
    if summary:
        tri.summary = summary
        tri.summary_source = "llm"
        tri.summary_model = str(llm["summary_model"]) if llm.get("summary_model") else None
    tri.llm_warnings.extend(str(w) for w in llm.get("warnings") or [])


def _triage_review(review: Review, store: BlobStore) -> None:
    """Rule triage, then (if VREVIEW_TRIAGE_LLM_MODELS is set) LLM-written comments. Best effort: any LLM
    failure leaves the rule text in place and adds a warning."""
    tri = triage(review)
    review.triage = tri

    models = settings().triage_llm_models
    if not models:
        return
    try:
        from vreview.triage_llm import write_comments
    except ImportError as exc:
        tri.llm_warnings.append(f"LLM comments unavailable: {exc}")
        return

    issues_by_id = {i.id: i for r in review.results for i in r.issues}
    src_lang, tgt_lang = _triage_langs(review)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="triage-llm")
    try:
        future = pool.submit(
            write_comments,
            tri.threads,
            issues_by_id,
            src_lang=src_lang,
            tgt_lang=tgt_lang,
            models=list(models),
            cache=store,
            cache_prefix="cache/triage_llm",
            verdict=tri.verdict,
        )
        llm = future.result(timeout=TRIAGE_LLM_TIMEOUT_S)
    except TimeoutError:
        tri.llm_warnings.append(f"LLM comments timed out after {TRIAGE_LLM_TIMEOUT_S:.0f}s; using rule-based text")
    except Exception as exc:
        logger.warning("LLM triage comments failed for run %s", review.run_id, exc_info=True)
        tri.llm_warnings.append(f"LLM comments failed ({type(exc).__name__}: {' '.join(str(exc).split())[:200]})")
    else:
        _merge_llm_text(tri, llm)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


@review_workflow.task(
    name="finalize",
    parents=[ingest_task, assemble_task],
    execution_timeout=timedelta(minutes=15),  # triage LLM wait (up to TRIAGE_LLM_TIMEOUT_S) + store + DB + webhook
    retries=3,
)
def finalize_task(input: ReviewInput, ctx: Context) -> Review:
    """Triages the review, stores review.json in blob store, updates DB via repo.save_review, and triggers webhook delivery."""
    from vreview.db import repo
    from vreview import webhooks

    _stage(input.run_id, "finalize")
    ingest_out: IngestOutput = ctx.task_output(ingest_task)
    media_refs = ingest_out.media
    assemble_out: AssembleOutput = ctx.task_output(assemble_task)
    results = assemble_out.results

    scores = [r.score for r in results if r.score is not None]
    overall_score = min(scores) if scores else None

    review = Review(
        run_id=input.run_id,
        brief=input.brief,
        media=media_refs,
        results=results,
        score=overall_score,
        created_at=datetime.now(timezone.utc),
    )

    store = get_store()
    _triage_review(review, store)

    key = review_key(input.run_id, "review.json")
    store.put_bytes(key, review.model_dump_json(indent=2).encode("utf-8"), "application/json")

    repo.save_review(review)
    webhooks.deliver(input.run_id)

    # Scratch cleanup for this run
    shutil.rmtree(settings().scratch_dir / input.run_id, ignore_errors=True)

    return review


@review_workflow.on_failure_task(
    name="on_failure",
    execution_timeout=timedelta(minutes=5),
)
def on_failure_task(input: ReviewInput, ctx: Context) -> None:
    """Marks run failed in the database on workflow error and cleans scratch dir."""
    from vreview.db import repo

    errors = getattr(ctx, "task_run_errors", None) or {}
    error_msg = "; ".join(f"{k}: {v}" for k, v in errors.items()) if errors else "Workflow execution failed"

    try:
        repo.mark_run(input.run_id, "failed", error=error_msg)
    except Exception as e:
        logger.error("Failed to mark run %s as failed: %s", input.run_id, e)

    shutil.rmtree(settings().scratch_dir / input.run_id, ignore_errors=True)
