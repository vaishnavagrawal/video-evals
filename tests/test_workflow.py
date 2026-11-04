"""Unit tests for vreview workflows and tasks."""
from __future__ import annotations

import inspect
import json
import sys
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hatchet_sdk import Context

from vreview.config import Settings
from vreview.evaluators.dubbing.models import DubConfig, TrackPlan, TrackRef
from vreview.jobs import MediaInput, ReviewInput
from vreview.schema import Brief, EvaluatorResult, Issue, MediaRef, Review
from vreview.storage.local import LocalStore
from vreview.workflows import review as review_module
from vreview.workflows.review import (
    AssembleOutput,
    DubbingPairsOutput,
    DubbingPlanOutput,
    IngestOutput,
    PairInput,
    TrackFeaturesInput,
    assemble_task,
    dubbing_features_task,
    dubbing_pairs_task,
    dubbing_plan_task,
    finalize_task,
    ingest_task,
    on_failure_task,
    pair_workflow,
    review_workflow,
    run_dubbing_pair,
    run_track_features,
    track_features_workflow,
)


def test_workflow_dag_structure():
    """Verify DAG structure, task names, parents, timeouts, retries, and concurrency."""
    assert review_workflow.name == "review"
    assert track_features_workflow.name == "dubbing-track-features"
    assert pair_workflow.name == "dubbing-pair"

    task_map = {t.name: t for t in review_workflow.tasks}
    expected_tasks = {"ingest", "dubbing_plan", "dubbing_features", "dubbing_pairs", "assemble", "finalize"}
    assert expected_tasks.issubset(set(task_map.keys()))

    # Ingest task
    ingest_t = task_map["ingest"]
    assert ingest_t.retries == 3
    assert ingest_t.execution_timeout == timedelta(minutes=15)
    assert len(ingest_t.parents) == 0

    # Dubbing plan task
    plan_t = task_map["dubbing_plan"]
    assert plan_t.retries == 1
    assert [p.name for p in plan_t.parents] == ["ingest"]

    # Features fan-out task
    feat_t = task_map["dubbing_features"]
    assert [p.name for p in feat_t.parents] == ["dubbing_plan"]

    # Pairs fan-out task
    pair_t = task_map["dubbing_pairs"]
    assert set(p.name for p in pair_t.parents) == {"dubbing_plan", "dubbing_features"}

    # Assemble task
    asm_t = task_map["assemble"]
    assert asm_t.retries == 1
    assert set(p.name for p in asm_t.parents) == {"dubbing_plan", "dubbing_pairs"}

    # Finalize task
    fin_t = task_map["finalize"]
    assert fin_t.retries == 3
    assert set(p.name for p in fin_t.parents) == {"ingest", "av_judge"}

    # On failure task
    assert review_workflow._on_failure_task is not None
    assert review_workflow._on_failure_task.name == "on_failure-on-failure"


def test_child_workflows_configuration():
    """Verify child workflows have appropriate timeouts and retries."""
    feat_tasks = {t.name: t for t in track_features_workflow.tasks}
    assert "track_features" in feat_tasks
    feat_task = feat_tasks["track_features"]
    assert feat_task.execution_timeout == timedelta(minutes=60)
    assert feat_task.retries == 2

    pair_tasks = {t.name: t for t in pair_workflow.tasks}
    assert "run_pair" in pair_tasks
    pair_task = pair_tasks["run_pair"]
    assert pair_task.execution_timeout == timedelta(minutes=30)
    assert pair_task.retries == 2


def test_ingest_task(tmp_path: Path):
    """Test ingest_task with a local store."""
    store = LocalStore(tmp_path / "blobs")
    scratch = tmp_path / "scratch"

    # Seed an upload file into store
    raw_content = b"fake video content for testing"
    store.put_bytes("uploads/up1/test.mp4", raw_content, "video/mp4")

    media_in = MediaInput(asset_id="asset1", role="original", key="uploads/up1/test.mp4")
    review_in = ReviewInput(
        run_id="run_test_ingest",
        brief=Brief(kind="dub"),
        media=[media_in],
    )

    ctx_mock = MagicMock(spec=Context)

    with patch("vreview.workflows.review.get_store", return_value=store), \
         patch("vreview.workflows.review.settings", return_value=Settings(scratch_dir=scratch)), \
         patch("vreview.db.repo.mark_run") as mock_mark:
        out = ingest_task._fn(review_in, ctx_mock)
        assert len(out.media) == 1
        ref = out.media[0]
        assert ref.role == "original"
        assert ref.key.startswith("media/")
        assert store.exists(ref.key)
        mock_mark.assert_called_once_with("run_test_ingest", "running", stage="ingest")


def test_dubbing_plan_skip_when_not_requested(tmp_path: Path):
    """Test dubbing_plan_task cleanly skipping when evaluators list excludes dubbing."""
    review_in = ReviewInput(
        run_id="run_skip",
        brief=Brief(kind="dub"),
        media=[],
        evaluators=[],  # Dubbing not requested
    )
    ctx_mock = MagicMock(spec=Context)
    ctx_mock.task_output.return_value = IngestOutput(media=[])
    out = dubbing_plan_task._fn(review_in, ctx_mock)
    assert out.skipped_result is not None
    assert out.skipped_result.status == "skipped"
    assert out.skipped_result.evaluator == "dubbing"
    assert "not requested" in (out.skipped_result.reason or "").lower()


def test_dubbing_plan_skip_when_not_applicable(tmp_path: Path):
    """Test dubbing_plan_task cleanly skipping when applies_to rejects inputs."""
    review_in = ReviewInput(
        run_id="run_skip_applies",
        brief=Brief(kind="generation"),  # kind is generation, not dub
        media=[
            MediaInput(asset_id="1", role="original", key="1.mp4"),
            MediaInput(asset_id="2", role="candidate", key="2.mp4"),
        ],
    )
    media_refs = [
        MediaRef(asset_id="1", role="original", key="media/1.mp4", sha256="1"),
        MediaRef(asset_id="2", role="candidate", key="media/2.mp4", sha256="2"),
    ]
    ctx_mock = MagicMock(spec=Context)
    ctx_mock.task_output.return_value = IngestOutput(media=media_refs)
    out = dubbing_plan_task._fn(review_in, ctx_mock)
    assert out.skipped_result is not None
    assert out.skipped_result.status == "skipped"
    assert "does not apply" in (out.skipped_result.reason or "").lower()


def test_assemble_skipped_result():
    """Test assemble_task returns skipped EvaluatorResult directly."""
    skipped = EvaluatorResult(
        evaluator="dubbing",
        version="1",
        status="skipped",
        reason="Skipped for test",
    )
    plan_out = DubbingPlanOutput(skipped_result=skipped)
    ctx_mock = MagicMock(spec=Context)
    ctx_mock.task_output.return_value = plan_out

    review_in = ReviewInput(run_id="run_asm", brief=Brief(kind="dub"), media=[])
    res = assemble_task._fn(review_in, ctx_mock)
    assert len(res.results) == 1
    assert res.results[0].status == "skipped"
    assert res.results[0].reason == "Skipped for test"


def test_finalize_task(tmp_path: Path):
    """Test finalize_task creates Review, writes review.json, calls repo and webhooks."""
    store = LocalStore(tmp_path / "blobs")
    scratch = tmp_path / "scratch"

    media_ref = MediaRef(asset_id="a1", role="original", key="media/abc.mp4", sha256="abc")
    eval_res = EvaluatorResult(
        evaluator="dubbing",
        version="1",
        status="ok",
        score=92.5,
    )

    review_in = ReviewInput(run_id="run_fin_1", brief=Brief(kind="dub"), media=[])
    ctx_mock = MagicMock(spec=Context)
    ctx_mock.task_output.side_effect = lambda task: IngestOutput(media=[media_ref]) if task.name == "ingest" else AssembleOutput(results=[eval_res])

    with patch("vreview.workflows.review.get_store", return_value=store), \
         patch("vreview.workflows.review.settings", return_value=Settings(scratch_dir=scratch)), \
         patch("vreview.db.repo.save_review") as mock_save, \
         patch("vreview.webhooks.deliver") as mock_deliver:

        review = finalize_task._fn(review_in, ctx_mock)
        assert len(review.results) == 1
        assert len(review.media) == 1

        # Check review.json stored in blob store
        assert store.exists("reviews/run_fin_1/review.json")
        saved_bytes = store.get_bytes("reviews/run_fin_1/review.json")
        saved_json = json.loads(saved_bytes.decode("utf-8"))
        assert saved_json["run_id"] == "run_fin_1"
        assert saved_json["score"] == 92.5

        mock_save.assert_called_once_with(review)
        mock_deliver.assert_called_once_with("run_fin_1")


def test_on_failure_task(tmp_path: Path):
    """Test on_failure_task marks the run failed and deletes scratch."""
    scratch = tmp_path / "scratch" / "run_fail_1"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "temp.txt").write_text("temporary data")

    review_in = ReviewInput(run_id="run_fail_1", brief=Brief(kind="dub"), media=[])
    ctx_mock = MagicMock(spec=Context)
    ctx_mock.task_run_errors = {"ingest": "Download timed out"}

    with patch("vreview.workflows.review.settings", return_value=Settings(scratch_dir=tmp_path / "scratch")), \
         patch("vreview.db.repo.mark_run") as mock_mark:
        on_failure_task._fn(review_in, ctx_mock)
        assert not scratch.exists()


# --- triage wiring in finalize ---------------------------------------------------------------------------------

def _issue(i: int, category: str, severity: str, t0: float, **details) -> Issue:
    return Issue(
        id=f"dubbing:hi->es:I{i:03d}", evaluator="dubbing", category=category, severity=severity, confidence=0.9,
        t0=t0, t1=t0 + 1.0, track="hi->es", description=f"{category} here", fix_hint=f"fix {category}",
        details={"source": "timing", **details},
    )


def _run_finalize(tmp_path: Path, *, models: list[str], brief: Brief | None = None):
    """Runs finalize_task on a result with 3 issues (2 threads: T001 blocker, T002 nit). Returns (review, store, save mock)."""
    store = LocalStore(tmp_path / "blobs")
    result = EvaluatorResult(
        evaluator="dubbing", version="1", status="ok", score=40.0,
        issues=[_issue(1, "EARLY_CUTOFF", "critical", 10.0), _issue(2, "OVERRUN", "major", 10.5),
                _issue(3, "EARLY_START", "minor", 60.0)],
    )
    review_in = ReviewInput(run_id="run_fin_t", brief=brief or Brief(kind="dub"), media=[])
    ctx = MagicMock(spec=Context)
    ctx.task_output.side_effect = lambda task: (
        IngestOutput(media=[]) if task.name == "ingest" else AssembleOutput(results=[result])
    )
    settings_obj = Settings(scratch_dir=tmp_path / "scratch", triage_llm_models=models)
    with patch("vreview.workflows.review.get_store", return_value=store), \
         patch("vreview.workflows.review.settings", return_value=settings_obj), \
         patch("vreview.db.repo.mark_run"), \
         patch("vreview.db.repo.save_review") as mock_save, \
         patch("vreview.webhooks.deliver"):
        review = finalize_task._fn(review_in, ctx)
    return review, store, mock_save


def test_finalize_runs_rule_triage_and_stores_it_everywhere(tmp_path: Path):
    with patch("vreview.triage_llm.write_comments", side_effect=AssertionError("LLM must be off by default")):
        review, store, mock_save = _run_finalize(tmp_path, models=[])

    tri = review.triage
    assert tri is not None and tri.verdict == "request_changes"
    assert [t.id for t in tri.threads] == ["T001", "T002"]
    assert tri.counts == {"blocker": 1, "should-fix": 0, "needs-human": 0, "nit": 1}
    assert tri.summary_source == "rule" and tri.llm_warnings == []
    assert all(t.comment_source == "rule" for t in tri.threads)

    # the issues carry their labels, and those are what gets persisted
    saved = mock_save.call_args.args[0]
    assert saved is review
    labels = {i.id: (i.priority, i.area, i.thread_id) for i in saved.results[0].issues}
    assert labels["dubbing:hi->es:I001"] == ("blocker", "sync", "T001")
    assert labels["dubbing:hi->es:I002"] == ("should-fix", "sync", "T001")
    assert labels["dubbing:hi->es:I003"] == ("nit", "sync", "T002")

    stored = json.loads(store.get_bytes("reviews/run_fin_t/review.json"))
    assert stored["triage"]["verdict"] == "request_changes"
    assert stored["results"][0]["issues"][0]["thread_id"] == "T001"
    assert stored["results"][0]["issues"][0]["tags"] == ["area:sync"]


def test_finalize_merges_llm_text_when_models_are_configured(tmp_path: Path):
    calls = []

    def fake_write_comments(threads, issues_by_id, **kwargs):
        calls.append((threads, issues_by_id, kwargs))
        return {
            "threads": {"T001": {"title": "LLM title", "comment": "LLM comment", "model": "gemini-flash"}},
            "summary": "LLM summary",
            "summary_model": "gemini-flash",
            "warnings": ["chunk 2 fell back to auto/best-fast"],
        }

    brief = Brief(kind="dub", source_language="hi", target_languages=["es"])
    with patch("vreview.triage_llm.write_comments", fake_write_comments):
        review, store, _ = _run_finalize(tmp_path, models=["gemini-flash", "auto/best-fast"], brief=brief)

    (threads, issues_by_id, kwargs), = calls
    from vreview import triage_llm

    inspect.signature(triage_llm.write_comments).bind(threads, issues_by_id, **kwargs)  # the real signature accepts this call
    assert [t.id for t in threads] == ["T001", "T002"]
    assert set(issues_by_id) == {"dubbing:hi->es:I001", "dubbing:hi->es:I002", "dubbing:hi->es:I003"}
    assert kwargs["models"] == ["gemini-flash", "auto/best-fast"]
    assert (kwargs["src_lang"], kwargs["tgt_lang"]) == ("hi", "es")
    assert kwargs["cache"] is store and kwargs["cache_prefix"] == "cache/triage_llm"
    assert kwargs["verdict"] == "request_changes"

    t1, t2 = review.triage.threads
    assert (t1.title, t1.comment, t1.comment_source, t1.comment_model) == ("LLM title", "LLM comment", "llm", "gemini-flash")
    # the LLM said nothing about T002: its rule text stays
    assert t2.comment_source == "rule" and t2.comment_model is None and "EARLY_START here" in t2.comment
    assert (review.triage.summary, review.triage.summary_source, review.triage.summary_model) == (
        "LLM summary", "llm", "gemini-flash")
    assert review.triage.llm_warnings == ["chunk 2 fell back to auto/best-fast"]
    stored = json.loads(store.get_bytes("reviews/run_fin_t/review.json"))
    assert stored["triage"]["threads"][0]["comment_source"] == "llm"


def test_finalize_keeps_rule_text_when_the_llm_returns_nothing_usable(tmp_path: Path):
    with patch("vreview.triage_llm.write_comments",
               return_value={"threads": {}, "summary": None, "warnings": ["no model answered"]}):
        review, _, _ = _run_finalize(tmp_path, models=["m"])
    assert review.triage.summary_source == "rule"
    assert all(t.comment_source == "rule" for t in review.triage.threads)
    assert review.triage.llm_warnings == ["no model answered"]


def test_finalize_survives_an_llm_exception(tmp_path: Path):
    with patch("vreview.triage_llm.write_comments", side_effect=RuntimeError("gateway exploded")):
        review, store, mock_save = _run_finalize(tmp_path, models=["m"])
    assert review.triage.verdict == "request_changes"
    assert review.triage.summary_source == "rule"
    assert all(t.comment_source == "rule" and t.comment for t in review.triage.threads)
    assert len(review.triage.llm_warnings) == 1
    assert "RuntimeError" in review.triage.llm_warnings[0] and "gateway exploded" in review.triage.llm_warnings[0]
    assert store.exists("reviews/run_fin_t/review.json")
    mock_save.assert_called_once()


def test_finalize_gives_up_on_a_slow_llm(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(review_module, "TRIAGE_LLM_TIMEOUT_S", 0.2)

    def slow(*args, **kwargs):
        time.sleep(1.0)
        return {"threads": {"T001": {"title": "late", "comment": "late"}}, "summary": "late", "warnings": []}

    with patch("vreview.triage_llm.write_comments", slow):
        started = time.monotonic()
        review, _, _ = _run_finalize(tmp_path, models=["m"])
        elapsed = time.monotonic() - started
    assert elapsed < 0.9
    assert "timed out" in review.triage.llm_warnings[0]
    assert review.triage.summary_source == "rule"
    assert all(t.comment_source == "rule" for t in review.triage.threads)


def test_finalize_survives_a_missing_triage_llm_module(tmp_path: Path):
    with patch.dict(sys.modules, {"vreview.triage_llm": None}):
        review, _, mock_save = _run_finalize(tmp_path, models=["m"])
    assert review.triage.verdict == "request_changes"
    assert review.triage.llm_warnings and "unavailable" in review.triage.llm_warnings[0]
    mock_save.assert_called_once()


def test_triage_languages_fall_back_to_the_single_dub_pair():
    pair = EvaluatorResult(evaluator="dubbing", version="1", status="ok", issues=[_issue(1, "OVERRUN", "major", 1.0)])
    base = dict(run_id="r", media=[], results=[pair], created_at=review_module.datetime.now(review_module.timezone.utc))
    assert review_module._triage_langs(Review(brief=Brief(kind="dub"), **base)) == ("hi", "es")
    declared = Brief(kind="dub", source_language="en", target_languages=["fr"])
    assert review_module._triage_langs(Review(brief=declared, **base)) == ("en", "fr")
    several = Brief(kind="dub", target_languages=["fr", "de"])
    assert review_module._triage_langs(Review(brief=several, **base)) == ("hi", "es")  # ambiguous brief -> the pair
    other = EvaluatorResult(evaluator="dubbing", version="1", status="ok",
                            issues=[_issue(1, "OVERRUN", "major", 1.0).model_copy(update={"track": "hi->fr"})])
    two_pairs = Review(brief=Brief(kind="dub"), **{**base, "results": [pair, other]})
    assert review_module._triage_langs(two_pairs) == (None, None)


def test_finalize_timeout_leaves_room_for_the_llm_wait():
    fin = {t.name: t for t in review_workflow.tasks}["finalize"]
    assert fin.execution_timeout >= timedelta(seconds=review_module.TRIAGE_LLM_TIMEOUT_S) + timedelta(minutes=2)


# ---------------------------------------------------------------------------
# av_judge task
# ---------------------------------------------------------------------------

def _av_ctx(results, media=None):
    from vreview.workflows.review import DubbingPlanOutput
    ctx = MagicMock()
    outs = {"ingest": IngestOutput(media=media or []), "dubbing_plan": DubbingPlanOutput(),
            "assemble": AssembleOutput(results=results)}
    ctx.task_output.side_effect = lambda task: outs[task.name]
    return ctx


def _dub_result():
    from vreview.schema import Issue
    iss = Issue(id="dubbing:hi->es:I001", evaluator="dubbing", category="EARLY_CUTOFF", severity="major",
                confidence=0.8, t0=1, t1=2, track="hi->es", description="d", fix_hint="f")
    return EvaluatorResult(evaluator="dubbing", version="1", status="ok", score=90, issues=[iss])


def test_av_judge_task_without_models_passes_results_through(tmp_path):
    from vreview.workflows.review import av_judge_task
    res = _dub_result()
    with patch("vreview.workflows.review.settings", return_value=Settings(_env_file=None, scratch_dir=tmp_path, av_judge_models=[])), \
         patch("vreview.db.repo.mark_run"), patch("vreview.pipeline_events.record"):
        out = av_judge_task._fn(ReviewInput(run_id="r1", brief=Brief(), media=[]), _av_ctx([res]))
    assert [r.evaluator for r in out.results] == ["dubbing"]


def test_av_judge_task_applies_verdicts_and_contains_failures(tmp_path):
    from vreview.evaluators import av_judge as av
    from vreview.workflows.review import av_judge_task
    res = _dub_result()
    review = {"dubbing:hi->es:I001": {"issue_id": "dubbing:hi->es:I001", "verdict": "dispute", "confidence": 0.9,
                                      "reason": "fine", "model": "meta:m"}}
    fake = av.AVJudgeOutput(result=EvaluatorResult(evaluator="av_judge", version="1", status="ok"), reviews=review)
    s = Settings(scratch_dir=tmp_path, av_judge_models=["meta:m"])
    with patch("vreview.workflows.review.settings", return_value=s), patch("vreview.db.repo.mark_run"), \
         patch("vreview.pipeline_events.record"), patch("vreview.workflows.review.get_store"), \
         patch.object(av, "run", return_value=fake):
        out = av_judge_task._fn(ReviewInput(run_id="r1", brief=Brief(), media=[]), _av_ctx([res]))
    assert [r.evaluator for r in out.results] == ["dubbing", "av_judge"]
    assert out.results[0].issues[0].details["av_review"]["verdict"] == "dispute"
    assert res.issues[0].confidence == 0.8                       # upstream output not mutated

    with patch("vreview.workflows.review.settings", return_value=s), patch("vreview.db.repo.mark_run"), \
         patch("vreview.pipeline_events.record"), patch("vreview.workflows.review.get_store"), \
         patch.object(av, "run", side_effect=RuntimeError("ffmpeg exploded")):
        out = av_judge_task._fn(ReviewInput(run_id="r1", brief=Brief(), media=[]), _av_ctx([res]))
    assert out.results[-1].status == "failed" and "ffmpeg exploded" in out.results[-1].reason
