"""Unit tests for vreview workflows and tasks."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from hatchet_sdk import Context

from vreview.config import Settings
from vreview.evaluators.dubbing.models import DubConfig, TrackPlan, TrackRef
from vreview.jobs import MediaInput, ReviewInput
from vreview.schema import Brief, EvaluatorResult, MediaRef, Review
from vreview.storage.local import LocalStore
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
    assert set(p.name for p in fin_t.parents) == {"ingest", "assemble"}

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
