"""Tests for pipeline events: the recorder, the GET /v1/reviews/{run_id}/pipeline endpoint, the MCP tool and the
review workflow's use of them.

Every test runs on SQLite and, when the compose Postgres is reachable, on a throwaway Postgres database too.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from fastapi.testclient import TestClient
from hatchet_sdk import Context
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from vreview import pipeline_events
from vreview.api.app import app
from vreview.config import Settings, settings
from vreview.db import repo
from vreview.db.models import Base, PipelineEventRow
from vreview.db.session import get_engine, get_sessionmaker
from vreview.evaluators.dubbing.models import DubConfig, PairResult, TrackFeatures, TrackRef
from vreview.jobs import ReviewInput
from vreview.schema import Brief, EvaluatorResult, MediaRef
from vreview.storage.local import LocalStore
from vreview.workflows.review import (
    DubbingPlanOutput,
    PairInput,
    TrackFeaturesInput,
    assemble_task,
    dubbing_plan_task,
    ingest_task,
    on_failure_task,
    run_dubbing_pair,
    run_track_features,
)

COMPOSE_PG_HOST = "localhost"
COMPOSE_PG_PORT = 55433
COMPOSE_PG_ADMIN = f"postgresql://vreview:vreview@{COMPOSE_PG_HOST}:{COMPOSE_PG_PORT}/vreview"


# --- database fixtures -------------------------------------------------------------------------------------------

@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def db_url(request, tmp_path_factory):
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path_factory.mktemp('pev') / 'pev.db'}"
        engine = create_engine(url)
        Base.metadata.create_all(bind=engine)
        engine.dispose()
        yield url
        return

    try:
        conn = psycopg.connect(COMPOSE_PG_ADMIN, autocommit=True)
    except Exception as exc:
        pytest.skip(f"Compose Postgres at {COMPOSE_PG_HOST}:{COMPOSE_PG_PORT} is unreachable: {exc}")
    dbname = f"test_pev_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE {dbname};")
    conn.close()

    url = f"postgresql+psycopg://vreview:vreview@{COMPOSE_PG_HOST}:{COMPOSE_PG_PORT}/{dbname}"
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    yield url

    engine.dispose()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    try:
        with psycopg.connect(COMPOSE_PG_ADMIN, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{dbname}' AND pid <> pg_backend_pid();"
            )
            cur.execute(f"DROP DATABASE {dbname};")
    except Exception:
        pass


@pytest.fixture(autouse=True)
def use_db(db_url, monkeypatch):
    monkeypatch.setenv("VREVIEW_DATABASE_URL", db_url)
    settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    yield
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture
def make_run():
    """Creates a run row for a tenant (events are tied to a run) and returns its id."""
    def make(tenant: str = "tenant_pev", status: str = "running") -> str:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        with get_sessionmaker()() as db:
            repo.create_run(db, tenant, run_id, brief={"kind": "dub"}, status=status)
        return run_id
    return make


def rows(run_id: str) -> dict[str, PipelineEventRow]:
    with get_sessionmaker()() as db:
        found = db.query(PipelineEventRow).filter(PipelineEventRow.run_id == run_id).all()
    return {r.node: r for r in found}


# --- record / upsert ---------------------------------------------------------------------------------------------

def test_record_creates_then_updates_one_row_per_node(make_run):
    run_id = make_run()
    pipeline_events.record(run_id, "ingest", status="running", label="Ingest media", detail={"files": 2, "gone": 1})
    first = rows(run_id)["ingest"]
    assert (first.status, first.label, first.kind, first.parent) == ("running", "Ingest media", "stage", None)
    assert first.finished_at is None and first.started_at is not None
    assert first.id.startswith("pev_")

    pipeline_events.record(run_id, "ingest", status="succeeded", detail={"issues": 0, "gone": None})
    found = rows(run_id)
    assert list(found) == ["ingest"]  # upsert, not a second row
    done = found["ingest"]
    assert done.status == "succeeded"
    assert done.label == "Ingest media"  # not given again, so kept
    assert done.started_at == first.started_at
    assert done.finished_at is not None
    assert done.detail == {"files": 2, "issues": 0}  # merged; None removed the key


def test_record_with_a_parent_is_a_child_node(make_run):
    run_id = make_run()
    pipeline_events.record(run_id, "feat", status="running", label="Features")
    pipeline_events.record(run_id, "feat/a", status="running", label="Original", parent="feat")
    found = rows(run_id)
    assert (found["feat"].kind, found["feat"].parent) == ("stage", None)
    assert (found["feat/a"].kind, found["feat/a"].parent) == ("child", "feat")


def test_record_terminal_status_on_first_call_sets_both_timestamps(make_run):
    run_id = make_run()
    pipeline_events.record(run_id, "av_judge", status="skipped", detail={"reason": "no models configured"})
    row = rows(run_id)["av_judge"]
    assert row.status == "skipped" and row.finished_at is not None
    assert row.label == "av_judge"  # no label given: the node name
    assert row.detail == {"reason": "no models configured"}


def test_a_finished_node_that_runs_again_is_a_retry(make_run):
    run_id = make_run()
    pipeline_events.record(run_id, "ingest", status="running")
    pipeline_events.record(run_id, "ingest", status="failed", detail={"error": "boom"})
    failed = rows(run_id)["ingest"]
    assert failed.status == "failed" and failed.detail["error"] == "boom"

    pipeline_events.record(run_id, "ingest", status="running")
    retry = rows(run_id)["ingest"]
    assert retry.status == "running" and retry.finished_at is None
    assert "error" not in retry.detail and retry.detail["attempt"] == 2
    assert retry.started_at >= failed.started_at

    pipeline_events.record(run_id, "ingest", status="running", detail={"files": 1})  # progress, not a retry
    assert rows(run_id)["ingest"].detail == {"attempt": 2, "files": 1}


def test_detail_is_made_json_safe(make_run):
    run_id = make_run()
    pipeline_events.record(
        run_id, "n", status="succeeded",
        detail={"nan": float("nan"), "path": Path("/tmp/x"), "tags": {"b"}, 7: "int key", "ok": 1.5},
    )
    # NaN is not valid JSON: it becomes None, and a None value is not stored
    assert rows(run_id)["n"].detail == {"path": "/tmp/x", "tags": ["b"], "7": "int key", "ok": 1.5}


# --- stage() -----------------------------------------------------------------------------------------------------

def test_stage_records_running_then_succeeded_with_detail(make_run):
    run_id = make_run()
    with pipeline_events.stage(run_id, "av_judge", "AV judge") as st:
        mid = rows(run_id)["av_judge"]
        assert (mid.status, mid.label, mid.finished_at) == ("running", "AV judge", None)
        st.update(model="meta:muse-spark-1.3")
        assert rows(run_id)["av_judge"].detail == {"model": "meta:muse-spark-1.3"}  # visible while running
        st.update(issues=12, cached=True)
    done = rows(run_id)["av_judge"]
    assert done.status == "succeeded" and done.finished_at is not None
    assert done.detail == {"model": "meta:muse-spark-1.3", "issues": 12, "cached": True}


def test_stage_records_failure_and_reraises(make_run):
    run_id = make_run()
    with pytest.raises(ValueError, match="boom"):
        with pipeline_events.stage(run_id, "finalize", "Finalize") as st:
            st.update(files=3)
            raise ValueError("boom\nwith   newline")
    row = rows(run_id)["finalize"]
    assert row.status == "failed" and row.finished_at is not None
    assert row.detail == {"files": 3, "error": "ValueError: boom with newline"}


def test_stage_error_text_is_capped(make_run):
    run_id = make_run()
    with pytest.raises(RuntimeError):
        with pipeline_events.stage(run_id, "n", "N"):
            raise RuntimeError("x" * 5000)
    assert len(rows(run_id)["n"].detail["error"]) == 500


def test_stage_can_end_as_skipped(make_run):
    run_id = make_run()
    with pipeline_events.stage(run_id, "av_judge", "AV judge") as st:
        st.skip("no video attached")
    row = rows(run_id)["av_judge"]
    assert row.status == "skipped" and row.detail == {"reason": "no video attached"}


def test_stage_child_nodes_nest_under_their_parent(make_run):
    run_id = make_run()
    with pipeline_events.stage(run_id, "features", "Features"):
        with pipeline_events.stage(run_id, "features/t1", "Track 1", parent="features"):
            pass
    found = rows(run_id)
    assert found["features/t1"].parent == "features" and found["features/t1"].kind == "child"
    assert found["features"].status == found["features/t1"].status == "succeeded"


def test_astage_succeeds_and_fails(make_run):
    run_id = make_run()

    async def scenario():
        async with pipeline_events.astage(run_id, "pairs", "Pairs") as st:
            st.update(pairs=2)
        with pytest.raises(KeyError):
            async with pipeline_events.astage(run_id, "assemble", "Assemble"):
                raise KeyError("missing")

    asyncio.run(scenario())
    found = rows(run_id)
    assert found["pairs"].status == "succeeded" and found["pairs"].detail == {"pairs": 2}
    assert found["assemble"].status == "failed" and "KeyError" in found["assemble"].detail["error"]


# --- never raises ------------------------------------------------------------------------------------------------

def test_database_errors_are_swallowed_and_logged(make_run, caplog):
    run_id = make_run()
    broken = MagicMock(side_effect=OperationalError("select 1", {}, Exception("connection refused")))
    with patch.object(pipeline_events, "get_sessionmaker", broken), caplog.at_level(logging.WARNING):
        pipeline_events.record(run_id, "ingest", status="running")  # does not raise
        pipeline_events.fail_running(run_id, "x")

        with pipeline_events.stage(run_id, "finalize", "Finalize") as st:  # the work still runs
            st.update(a=1)
            result = "review"
        assert result == "review"

        with pytest.raises(ValueError, match="real failure"):  # the caller's exception, not the DB's
            with pipeline_events.stage(run_id, "finalize", "Finalize"):
                raise ValueError("real failure")

        async def scenario():
            async with pipeline_events.astage(run_id, "pairs", "Pairs"):
                pass

        asyncio.run(scenario())
    assert "Pipeline event not recorded" in caplog.text
    assert rows(run_id) == {}


def test_recording_for_an_unknown_run_does_not_raise():
    pipeline_events.record("run_does_not_exist", "ingest", status="running")  # FK violation on Postgres


# --- fail_running ------------------------------------------------------------------------------------------------

def test_fail_running_marks_only_running_nodes_failed(make_run):
    run_id = make_run()
    other = make_run()
    pipeline_events.record(run_id, "ingest", status="succeeded")
    pipeline_events.record(run_id, "pairs", status="running")
    pipeline_events.record(run_id, "pairs/a", status="running", parent="pairs", detail={"error": "own error"})
    pipeline_events.record(other, "pairs", status="running")

    pipeline_events.fail_running(run_id, "pairs: timed out")
    found = rows(run_id)
    assert found["ingest"].status == "succeeded"
    assert found["pairs"].status == "failed" and found["pairs"].finished_at is not None
    assert found["pairs"].detail["error"] == "pairs: timed out"
    assert found["pairs/a"].detail["error"] == "own error"  # an existing error is kept
    assert rows(other)["pairs"].status == "running"  # other runs untouched


# --- list_events -------------------------------------------------------------------------------------------------

def test_list_events_is_tenant_scoped_and_ordered_by_start(make_run):
    run_id = make_run("tenant_a")
    for node in ("one", "two", "three"):
        pipeline_events.record(run_id, node, status="succeeded")
    with get_sessionmaker()() as db:
        assert [e.node for e in pipeline_events.list_events(db, "tenant_a", run_id)] == ["one", "two", "three"]
        assert pipeline_events.list_events(db, "tenant_b", run_id) == []
        assert pipeline_events.list_events(db, "tenant_a", "run_nope") == []


# --- endpoint ----------------------------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    """The real auth dependency, with the token standing in for the tenant (JWT checking has its own tests)."""
    monkeypatch.setattr("vreview.api.auth.verify_jwt", lambda token, jwks_url=None: (token, {}))
    with TestClient(app) as test_client:
        yield test_client


def auth(tenant: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tenant}"}


def test_pipeline_endpoint_returns_nodes_in_start_order(client, make_run):
    run_id = make_run("tenant_a", status="running")
    with get_sessionmaker()() as db:
        repo.mark_run(run_id, "running", stage="dubbing_features", db=db)
    pipeline_events.record(run_id, "ingest", status="running", label="Ingest media")
    pipeline_events.record(run_id, "ingest", status="succeeded", detail={"files": 2})
    pipeline_events.record(run_id, "dubbing_features", status="running", label="Extract speech features")
    pipeline_events.record(
        run_id, "dubbing_features/t1", status="running", label="Dub · es", parent="dubbing_features",
        detail={"model": "groq/whisper-large-v3", "cached": False},
    )

    resp = client.get(f"/v1/reviews/{run_id}/pipeline", headers=auth("tenant_a"))
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"run_id", "status", "stage", "server_time", "nodes"}
    assert (body["run_id"], body["status"], body["stage"]) == (run_id, "running", "dubbing_features")
    datetime.fromisoformat(body["server_time"])

    nodes = body["nodes"]
    assert [n["node"] for n in nodes] == ["ingest", "dubbing_features", "dubbing_features/t1"]
    assert all(
        set(n) == {"node", "parent", "label", "kind", "status", "started_at", "finished_at", "duration_s", "detail"}
        for n in nodes
    )
    ingest, features, child = nodes
    assert (ingest["status"], ingest["kind"], ingest["parent"], ingest["detail"]) == ("succeeded", "stage", None, {"files": 2})
    assert ingest["finished_at"] is not None and ingest["duration_s"] >= 0
    # timestamps are UTC with an offset, whatever the database returns
    assert datetime.fromisoformat(ingest["started_at"]).tzinfo is not None
    assert (features["status"], features["finished_at"]) == ("running", None)
    assert features["duration_s"] >= 0  # a running node's age
    assert (child["parent"], child["kind"], child["label"]) == ("dubbing_features", "child", "Dub · es")
    assert child["detail"] == {"model": "groq/whisper-large-v3", "cached": False}


def test_pipeline_endpoint_is_empty_for_a_run_without_events(client, make_run):
    run_id = make_run("tenant_a", status="succeeded")
    resp = client.get(f"/v1/reviews/{run_id}/pipeline", headers=auth("tenant_a"))
    assert resp.status_code == 200
    assert resp.json()["nodes"] == [] and resp.json()["status"] == "succeeded"


def test_pipeline_endpoint_is_tenant_scoped(client, make_run):
    run_id = make_run("tenant_a")
    pipeline_events.record(run_id, "ingest", status="running")

    other = client.get(f"/v1/reviews/{run_id}/pipeline", headers=auth("tenant_b"))
    assert other.status_code == 404
    assert other.json()["detail"] == "Review run not found"
    assert "ingest" not in other.text

    assert client.get("/v1/reviews/run_nope/pipeline", headers=auth("tenant_a")).status_code == 404
    assert client.get(f"/v1/reviews/{run_id}/pipeline").status_code == 401


def test_mcp_get_pipeline_tool(make_run):
    from fastapi import HTTPException

    from vreview.api import mcp as mcp_module

    run_id = make_run("tenant_mcp")
    pipeline_events.record(run_id, "ingest", status="succeeded", label="Ingest media")

    async def call(tenant: str):
        tool = await mcp_module.mcp_server.get_tool("get_pipeline")
        with patch.object(mcp_module, "_extract_tenant", return_value=tenant):
            return tool.fn(run_id=run_id)

    out = asyncio.run(call("tenant_mcp"))
    assert out["run_id"] == run_id and [n["node"] for n in out["nodes"]] == ["ingest"]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(call("someone_else"))
    assert exc.value.status_code == 404


# --- the review workflow emits events ----------------------------------------------------------------------------

def _track(role: str, lang: str, slug: str) -> TrackRef:
    media = MediaRef(asset_id=f"a_{slug}", role="original" if role == "original" else "candidate",
                     key=f"media/{slug}.mp4", sha256=slug * 4)
    return TrackRef(media=media, audio_index=0, role=role, slug=slug, lang_tag=lang, wav_key=f"w/{slug}.wav")


def test_ingest_and_assemble_tasks_record_stage_nodes(make_run, tmp_path: Path):
    run_id = make_run()
    store = LocalStore(tmp_path / "blobs")
    store.put_bytes("uploads/up1/test.mp4", b"video", "video/mp4")
    review_in = ReviewInput(
        run_id=run_id, brief=Brief(kind="dub"),
        media=[{"asset_id": "a1", "role": "original", "key": "uploads/up1/test.mp4"}],
    )
    ctx = MagicMock(spec=Context)

    with patch("vreview.workflows.review.get_store", return_value=store), \
         patch("vreview.workflows.review.settings", return_value=Settings(scratch_dir=tmp_path / "scratch")):
        ingest_task._fn(review_in, ctx)

    skipped = EvaluatorResult(evaluator="dubbing", version="1", status="skipped", reason="Evaluator 'dubbing' not requested")
    ctx.task_output.return_value = DubbingPlanOutput(skipped_result=skipped)
    assemble_task._fn(review_in, ctx)

    found = rows(run_id)
    assert (found["ingest"].status, found["ingest"].label, found["ingest"].detail) == ("succeeded", "Ingest media", {"files": 1})
    assert found["assemble"].status == "skipped"
    assert found["assemble"].detail["reason"] == "Evaluator 'dubbing' not requested"


def test_dubbing_plan_task_records_skipped_when_not_requested(make_run):
    run_id = make_run()
    review_in = ReviewInput(run_id=run_id, brief=Brief(kind="dub"), media=[], evaluators=[])
    ctx = MagicMock(spec=Context)
    ctx.task_output.return_value = MagicMock(media=[])
    dubbing_plan_task._fn(review_in, ctx)
    row = rows(run_id)["dubbing_plan"]
    assert row.status == "skipped" and "not requested" in row.detail["reason"]


def test_a_failing_task_records_a_failed_node(make_run):
    run_id = make_run()
    review_in = ReviewInput(run_id=run_id, brief=Brief(kind="dub"), media=[])
    ctx = MagicMock(spec=Context)
    ctx.task_output.side_effect = RuntimeError("upstream output missing")
    with pytest.raises(RuntimeError):
        assemble_task._fn(review_in, ctx)
    row = rows(run_id)["assemble"]
    assert row.status == "failed" and "upstream output missing" in row.detail["error"]


def test_on_failure_task_fails_the_nodes_still_running(make_run, tmp_path: Path):
    run_id = make_run()
    pipeline_events.record(run_id, "ingest", status="succeeded")
    pipeline_events.record(run_id, "dubbing_pairs", status="running")
    pipeline_events.record(run_id, "dubbing_pairs/x", status="running", parent="dubbing_pairs")

    review_in = ReviewInput(run_id=run_id, brief=Brief(kind="dub"), media=[])
    ctx = MagicMock(spec=Context)
    ctx.task_run_errors = {"dubbing_pairs": "timed out"}
    with patch("vreview.workflows.review.settings", return_value=Settings(scratch_dir=tmp_path / "scratch")):
        on_failure_task._fn(review_in, ctx)

    found = rows(run_id)
    assert found["ingest"].status == "succeeded"
    assert found["dubbing_pairs"].status == found["dubbing_pairs/x"].status == "failed"
    assert "timed out" in found["dubbing_pairs"].detail["error"]


def test_track_features_child_records_a_child_node_and_cached_flag(make_run, tmp_path: Path):
    run_id = make_run()
    store = LocalStore(tmp_path / "blobs")
    track = _track("dub", "es", "dub1")
    cfg = DubConfig(asr_cloud_model="groq/whisper-large-v3")
    features = TrackFeatures(track=track, asr_key="asr.json")

    def computed(store_, track_, cfg_, scratch):
        store_.put_bytes("cache/features.json", b"{}", "application/json")
        return features

    def cached(store_, track_, cfg_, scratch):
        return features

    for fake, expected in ((computed, False), (cached, True)):
        with patch("vreview.workflows.review.get_store", return_value=store), \
             patch("vreview.workflows.review.settings", return_value=Settings(scratch_dir=tmp_path / "scratch")), \
             patch("vreview.evaluators.dubbing.stages.track_features", fake):
            out = run_track_features._fn(TrackFeaturesInput(run_id=run_id, track=track, cfg=cfg), MagicMock(spec=Context))
        assert out is features
        row = rows(run_id)["dubbing_features/dub1"]
        assert (row.status, row.parent, row.kind) == ("succeeded", "dubbing_features", "child")
        assert row.label == "Dub · es"
        assert row.detail["model"] == "groq/whisper-large-v3" and row.detail["lang"] == "es"
        assert row.detail["cached"] is expected


def test_pair_child_records_score_issues_models_and_cached_flag(make_run, tmp_path: Path):
    run_id = make_run()
    store = LocalStore(tmp_path / "blobs")
    src = TrackFeatures(track=_track("original", "hi", "src1"), asr_key="a")
    dub = TrackFeatures(track=_track("dub", "es", "dub1"), asr_key="b")
    cfg = DubConfig(llm_models=["meta:muse-spark-1.3", "gemini-flash"])
    result = PairResult(pair="hi->es", report_key="r.json", score=71.26, issues=[])

    def computed(store_, run_id_, src_, dub_, cfg_, scratch):
        store_.get_file("media/src1.mp4", scratch / "m.mp4")
        return result

    def cached(store_, run_id_, src_, dub_, cfg_, scratch):
        return result

    store.put_bytes("media/src1.mp4", b"x", "video/mp4")
    for fake, expected in ((computed, False), (cached, True)):
        with patch("vreview.workflows.review.get_store", return_value=store), \
             patch("vreview.workflows.review.settings", return_value=Settings(scratch_dir=tmp_path / "scratch")), \
             patch("vreview.evaluators.dubbing.stages.run_pair", fake):
            run_dubbing_pair._fn(PairInput(run_id=run_id, src=src, dub=dub, cfg=cfg), MagicMock(spec=Context))
        row = rows(run_id)["dubbing_pairs/dub1"]
        assert (row.status, row.parent, row.kind) == ("succeeded", "dubbing_pairs", "child")
        assert row.label == "Compare hi → es"
        assert row.detail["models"] == ["meta:muse-spark-1.3", "gemini-flash"]
        assert (row.detail["score"], row.detail["issues"], row.detail["cached"]) == (71.3, 0, expected)
