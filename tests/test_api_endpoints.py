"""Integration tests for vreview REST API endpoints."""
from __future__ import annotations

import http.server
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator
from unittest.mock import AsyncMock, patch

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from vreview.api.app import app
from vreview.config import settings
from vreview.db import repo
from vreview.db.models import Base
from vreview.db.session import get_engine, get_sessionmaker
from vreview.schema import Brief, EvaluatorResult, Issue, Review


COMPOSE_PG_HOST = "localhost"
COMPOSE_PG_PORT = 55433
COMPOSE_PG_URL = f"postgresql+psycopg://vreview:vreview@{COMPOSE_PG_HOST}:{COMPOSE_PG_PORT}/vreview"


@pytest.fixture(scope="session")
def test_db():
    """Sets up a throwaway database on the compose Postgres for the test session."""
    try:
        conn = psycopg.connect(
            f"postgresql://vreview:vreview@{COMPOSE_PG_HOST}:{COMPOSE_PG_PORT}/vreview",
            autocommit=True,
        )
    except Exception as exc:
        pytest.skip(f"Compose Postgres at {COMPOSE_PG_HOST}:{COMPOSE_PG_PORT} is unreachable: {exc}")

    test_dbname = f"test_vreview_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE {test_dbname};")
    conn.close()

    db_url = f"postgresql+psycopg://vreview:vreview@{COMPOSE_PG_HOST}:{COMPOSE_PG_PORT}/{test_dbname}"

    # Create tables
    engine = create_engine(db_url)
    Base.metadata.create_all(bind=engine)

    yield db_url

    # Teardown: drop test database
    engine.dispose()
    try:
        conn = psycopg.connect(
            f"postgresql://vreview:vreview@{COMPOSE_PG_HOST}:{COMPOSE_PG_PORT}/vreview",
            autocommit=True,
        )
        with conn.cursor() as cur:
            # Terminate connections to test db
            cur.execute(
                f"""
                SELECT pg_terminate_backend(pg_stat_activity.pid)
                FROM pg_stat_activity
                WHERE pg_stat_activity.datname = '{test_dbname}'
                  AND pid <> pg_backend_pid();
                """
            )
            cur.execute(f"DROP DATABASE {test_dbname};")
        conn.close()
    except Exception:
        pass


@pytest.fixture(scope="session")
def ed25519_server():
    """Starts a local HTTP server serving JWKS."""
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    alg = jwt.algorithms.get_default_algorithms()["EdDSA"]
    jwk_dict = alg.to_jwk(public_key, as_dict=True)
    jwk_dict["kid"] = "api-test-key"
    jwk_dict["alg"] = "EdDSA"
    jwk_dict["use"] = "sig"

    jwks_bytes = json.dumps({"keys": [jwk_dict]}).encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(jwks_bytes)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield {
        "private_key": private_key,
        "kid": "api-test-key",
        "url": f"http://127.0.0.1:{port}/.well-known/jwks.json",
    }

    server.shutdown()
    server.server_close()


def generate_token(ed25519_server, tenant: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": tenant,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=2)).timestamp()),
    }
    return jwt.encode(
        payload,
        ed25519_server["private_key"],
        algorithm="EdDSA",
        headers={"kid": ed25519_server["kid"]},
    )


@pytest.fixture(autouse=True)
def setup_env(test_db, ed25519_server, tmp_path: Path, monkeypatch):
    """Sets database, auth, and store env vars for tests."""
    monkeypatch.setenv("VREVIEW_DATABASE_URL", test_db)
    monkeypatch.setenv("VREVIEW_AUTH_JWKS_URL", ed25519_server["url"])
    monkeypatch.setenv("VREVIEW_STORE", "local")
    monkeypatch.setenv("VREVIEW_LOCAL_STORE_ROOT", str(tmp_path / "blobs"))
    settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    from vreview.storage import get_store
    get_store.cache_clear()
    from vreview.api.auth import _get_jwk_client
    _get_jwk_client.cache_clear()


def test_uploads_lifecycle_and_tenant_isolation(ed25519_server):
    token_a = generate_token(ed25519_server, tenant="tenant_A")
    token_b = generate_token(ed25519_server, tenant="tenant_B")

    with TestClient(app) as client:
        # 1. POST /v1/uploads for tenant A
        # Size = 35 MiB -> ceil(35/16) = 3 parts
        size = 35 * 1024 * 1024
        upload_resp = client.post(
            "/v1/uploads",
            json={
                "filename": "sample.mp4",
                "size": size,
                "content_type": "video/mp4",
                "role": "candidate",
            },
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert upload_resp.status_code == 201
        data = upload_resp.json()
        asset_id = data["asset_id"]
        upload_id = data["upload_id"]
        assert len(data["parts"]) == 3
        assert data["part_size"] == 16 * 1024 * 1024
        assert data["parts"][0]["part_number"] == 1
        # Write mock uploaded data to the presigned file URLs
        for p in data["parts"]:
            part_path = Path(p["url"].replace("file://", ""))
            part_path.parent.mkdir(parents=True, exist_ok=True)
            part_path.write_bytes(b"test chunk data")

        # 2. Tenant B cannot complete tenant A's upload (404)
        complete_payload = {
            "upload_id": upload_id,
            "parts": [{"PartNumber": p["part_number"], "ETag": f"etag_{p['part_number']}"} for p in data["parts"]],
        }
        res_b = client.post(
            f"/v1/uploads/{asset_id}/complete",
            json=complete_payload,
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert res_b.status_code == 404

        # 3. Tenant A can complete the upload
        res_a = client.post(
            f"/v1/uploads/{asset_id}/complete",
            json=complete_payload,
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert res_a.status_code == 200
        assert res_a.json() == {"asset_id": asset_id, "status": "completed"}


def test_reviews_enqueue_and_tenant_isolation(ed25519_server):
    token_a = generate_token(ed25519_server, tenant="tenant_A")
    token_b = generate_token(ed25519_server, tenant="tenant_B")

    with TestClient(app) as client:
        # Create an asset for tenant A
        up_res = client.post(
            "/v1/uploads",
            json={"filename": "dub.mp4", "size": 1024 * 1024},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        asset_id = up_res.json()["asset_id"]

        # Mock enqueue seam
        with patch("vreview.api.services.enqueue_review_workflow", new_callable=AsyncMock) as mock_enqueue:
            mock_enqueue.return_value = "hatchet_run_12345"

            # 1. POST /v1/reviews for tenant A
            review_resp = client.post(
                "/v1/reviews",
                json={
                    "assets": [{"asset_id": asset_id, "role": "candidate"}],
                    "brief": {
                        "kind": "dub",
                        "source_language": "en",
                        "target_languages": ["es"],
                    },
                    "evaluators": ["dubbing"],
                },
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert review_resp.status_code == 202
            data = review_resp.json()
            run_id = data["run_id"]
            assert data["status"] == "queued"
            assert data["hatchet_run_id"] == "hatchet_run_12345"
            assert mock_enqueue.called

        # 2. Tenant isolation on GET /v1/reviews/{id}: Tenant B gets 404
        get_b = client.get(f"/v1/reviews/{run_id}", headers={"Authorization": f"Bearer {token_b}"})
        assert get_b.status_code == 404

        # 3. Tenant A can get the review status
        get_a = client.get(f"/v1/reviews/{run_id}", headers={"Authorization": f"Bearer {token_a}"})
        assert get_a.status_code == 200
        assert get_a.json()["run_id"] == run_id
        assert get_a.json()["status"] == "queued"

        # 4. Ingest review result via save_review (as Hatchet finalize step does)
        rev = Review(
            run_id=run_id,
            brief=Brief(kind="dub"),
            media=[],
            results=[
                EvaluatorResult(
                    evaluator="dubbing",
                    version="1",
                    status="ok",
                    score=85.0,
                    issues=[
                        Issue(
                            id="dubbing:pair1:001",
                            evaluator="dubbing",
                            category="audio",
                            severity="critical",
                            confidence=0.95,
                            t0=0.0,
                            t1=2.5,
                            track="pair1",
                            description="Audio distortion",
                            fix_hint="Re-record",
                        ),
                        Issue(
                            id="dubbing:pair1:002",
                            evaluator="dubbing",
                            category="translation",
                            severity="minor",
                            confidence=0.7,
                            t0=5.0,
                            t1=7.0,
                            track="pair1",
                            description="Minor wording mismatch",
                            fix_hint="Adjust phrasing",
                        ),
                    ],
                )
            ],
            score=85.0,
            created_at=datetime.now(timezone.utc),
        )
        repo.save_review(rev)

        # 5. GET /v1/reviews/{id} now returns completed status, score, and body
        get_done = client.get(f"/v1/reviews/{run_id}", headers={"Authorization": f"Bearer {token_a}"})
        assert get_done.status_code == 200
        done_data = get_done.json()
        assert done_data["status"] == "succeeded"
        assert done_data["score"] == 85.0
        assert done_data["review"]["schema_version"] == "vreview.v1"

        # 6. GET /v1/reviews/{id}/issues with severity filter
        all_issues = client.get(
            f"/v1/reviews/{run_id}/issues",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert len(all_issues.json()) == 2

        crit_issues = client.get(
            f"/v1/reviews/{run_id}/issues?severity=critical",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert len(crit_issues.json()) == 1
        assert crit_issues.json()[0]["id"] == "dubbing:pair1:001"
        assert crit_issues.json()[0]["severity"] == "critical"

        # Tenant B gets empty or 404 for issues
        issues_b = client.get(
            f"/v1/reviews/{run_id}/issues",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert issues_b.status_code == 404

        # 7. POST feedback on an issue
        fb_resp = client.post(
            f"/v1/reviews/{run_id}/issues/dubbing:pair1:001/feedback",
            json={"verdict": "accept", "comment": "Valid finding"},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert fb_resp.status_code == 200
        assert fb_resp.json()["status"] == "ok"
        assert "feedback_id" in fb_resp.json()

        # Tenant B cannot provide feedback on tenant A's issue (404)
        fb_b = client.post(
            f"/v1/reviews/{run_id}/issues/dubbing:pair1:001/feedback",
            json={"verdict": "false_positive"},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert fb_b.status_code == 404

        # 8. GET /v1/reviews (paginated list)
        list_resp = client.get(
            "/v1/reviews?limit=10&offset=0",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert list_resp.status_code == 200
        list_data = list_resp.json()
        assert list_data["total"] >= 1
        assert any(item["run_id"] == run_id for item in list_data["items"])


def test_artifact_presigned_url(ed25519_server):
    token_a = generate_token(ed25519_server, tenant="tenant_A")
    token_b = generate_token(ed25519_server, tenant="tenant_B")

    with TestClient(app) as client:
        # Create a run in DB
        with get_sessionmaker()() as db:
            repo.create_run(
                db=db,
                tenant="tenant_A",
                run_id="run_art_123",
                brief={"kind": "dub"},
                status="succeeded",
            )

        # GET JSON url
        art_resp = client.get(
            "/v1/reviews/run_art_123/artifacts/dubbing/report.json",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert art_resp.status_code == 200
        assert "url" in art_resp.json()

        # GET redirect
        art_redir = client.get(
            "/v1/reviews/run_art_123/artifacts/dubbing/report.json?redirect=true",
            headers={"Authorization": f"Bearer {token_a}"},
            follow_redirects=False,
        )
        assert art_redir.status_code == 307
        assert "location" in art_redir.headers

        # Tenant B gets 404
        art_b = client.get(
            "/v1/reviews/run_art_123/artifacts/dubbing/report.json",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert art_b.status_code == 404


# ---------------------------------------------------------------------------------------------------------------
# Triage, media, upload validation, CORS
# ---------------------------------------------------------------------------------------------------------------

from vreview.config import Settings  # noqa: E402
from vreview.schema import MediaRef  # noqa: E402
from vreview.triage import triage  # noqa: E402


def _auth(ed25519_server, tenant: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {generate_token(ed25519_server, tenant)}"}


def _issue(n: int, category: str, severity: str, t0: float, *, confidence: float = 0.9, source: str = "timing",
           speaking: bool | None = None) -> Issue:
    return Issue(
        id=f"dubbing:hi->es:I{n:03d}", evaluator="dubbing", category=category, severity=severity,
        confidence=confidence, t0=t0, t1=t0 + 2.0, track="hi->es", description=f"{category} at {t0}",
        fix_hint=f"fix {category}",
        details={"source": source, "original_text": f"orig {n}", "dubbed_text": f"dub {n}",
                 "visual": None if speaking is None else {"speaking_likely": speaking}},
    )


def _triage_review(run_id: str, media: list[MediaRef] | None = None, *, with_triage: bool = True) -> Review:
    """T001 {I001 blocker, I002 should-fix} / T002 should-fix / T003 needs-human / T004 nit."""
    issues = [
        _issue(1, "EARLY_CUTOFF", "critical", 10.0, speaking=True),
        _issue(2, "OVERRUN", "major", 11.0),
        _issue(3, "MEANING_MISMATCH", "major", 40.0, source="semantic"),
        _issue(4, "TONE_MISMATCH", "minor", 80.0, confidence=0.5, source="semantic"),
        _issue(5, "EARLY_START", "minor", 120.0),
    ]
    review = Review(
        run_id=run_id, brief=Brief(kind="dub"), media=media or [], score=40.0,
        results=[EvaluatorResult(evaluator="dubbing", version="1", status="ok", score=40.0, issues=issues)],
        created_at=datetime.now(timezone.utc),
    )
    if with_triage:
        review.triage = triage(review)
    return review


def _seed_run(tenant: str, run_id: str, media: list[dict] | None = None, status: str = "succeeded") -> None:
    with get_sessionmaker()() as db:
        repo.create_run(db=db, tenant=tenant, run_id=run_id, brief={"kind": "dub"}, status=status, media=media)


def _new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:10]}"


def _upload(client, headers, filename: str, role: str, content_type: str | None = None):
    payload = {"filename": filename, "size": 1024, "role": role}
    if content_type is not None:
        payload["content_type"] = content_type
    return client.post("/v1/uploads", json=payload, headers=headers)


# --- GET /reviews/{run_id}: triage present, computed for old reviews -------------------------------------------

def test_get_review_returns_stored_triage(ed25519_server):
    h = _auth(ed25519_server, "tenant_A")
    run_id = _new_run_id()
    _seed_run("tenant_A", run_id)
    repo.save_review(_triage_review(run_id))

    with TestClient(app) as client:
        body = client.get(f"/v1/reviews/{run_id}", headers=h).json()
    tri = body["review"]["triage"]
    assert tri["verdict"] == "request_changes"
    assert tri["counts"] == {"blocker": 1, "should-fix": 1, "needs-human": 1, "nit": 1}
    assert [t["id"] for t in tri["threads"]] == ["T001", "T002", "T003", "T004"]
    assert tri["threads"][0]["issue_ids"] == ["dubbing:hi->es:I001", "dubbing:hi->es:I002"]
    assert tri["threads"][0]["comment_source"] == "rule" and tri["summary_source"] == "rule"
    first_issue = body["review"]["results"][0]["issues"][0]
    assert (first_issue["priority"], first_issue["area"], first_issue["thread_id"]) == ("blocker", "sync", "T001")


def test_get_review_computes_rule_triage_for_reviews_stored_without_one(ed25519_server):
    h = _auth(ed25519_server, "tenant_A")
    run_id = _new_run_id()
    _seed_run("tenant_A", run_id)
    old = _triage_review(run_id, with_triage=False)
    repo.save_review(old)
    with get_sessionmaker()() as db:  # simulate the pre-triage shape: strip the new keys from the stored body
        row = db.get(repo.ReviewRow, run_id)
        body = json.loads(json.dumps(row.body))
        body.pop("triage")
        for issue in body["results"][0]["issues"]:
            for key in ("priority", "tags", "area", "thread_id"):
                issue.pop(key, None)
        row.body = body
        db.commit()

    with TestClient(app) as client:
        resp = client.get(f"/v1/reviews/{run_id}", headers=h)
    assert resp.status_code == 200
    tri = resp.json()["review"]["triage"]
    assert tri["verdict"] == "request_changes"
    assert tri["counts"]["blocker"] == 1 and len(tri["threads"]) == 4
    assert tri["summary_source"] == "rule"
    assert resp.json()["review"]["results"][0]["issues"][0]["thread_id"] == "T001"

    with get_sessionmaker()() as db:  # computed on the fly, not written back
        assert "triage" not in db.get(repo.ReviewRow, run_id).body


# --- GET /reviews: verdict, counts, filenames ------------------------------------------------------------------

def test_list_reviews_has_verdict_counts_and_filenames(ed25519_server):
    h = _auth(ed25519_server, "tenant_L")
    media = [
        MediaRef(asset_id="ast_o", role="original", key="media/o.mp4", sha256="o", filename="original.mp4"),
        MediaRef(asset_id="ast_r", role="reference", key="media/r.png", sha256="r", filename="ref.png"),
        MediaRef(asset_id="ast_c", role="candidate", key="media/c.mp4", sha256="c", filename="dub.mp4"),
    ]
    done, queued, legacy = _new_run_id(), _new_run_id(), _new_run_id()
    run_media = [{"asset_id": m.asset_id, "role": m.role, "filename": m.filename} for m in media]
    _seed_run("tenant_L", done, media=run_media)
    repo.save_review(_triage_review(done, media))
    _seed_run("tenant_L", queued, media=run_media, status="queued")
    _seed_run("tenant_L", legacy, media=None)  # created before runs.media existed: names come from the review
    repo.save_review(_triage_review(legacy, media, with_triage=False))

    with TestClient(app) as client:
        items = {i["run_id"]: i for i in client.get("/v1/reviews?limit=100", headers=h).json()["items"]}

    expected_counts = {"blocker": 1, "should-fix": 1, "needs-human": 1, "nit": 1}
    assert items[done]["verdict"] == "request_changes" and items[done]["counts"] == expected_counts
    assert items[done]["filenames"] == ["dub.mp4", "original.mp4", "ref.png"]  # candidate first
    assert items[queued]["verdict"] is None and items[queued]["counts"] is None
    assert items[queued]["filenames"] == ["dub.mp4", "original.mp4", "ref.png"]
    assert items[legacy]["verdict"] == "request_changes" and items[legacy]["counts"] == expected_counts
    assert items[legacy]["filenames"] == ["dub.mp4", "original.mp4", "ref.png"]


# --- GET /reviews/{run_id}/issues: new filters and fields -------------------------------------------------------

def test_issues_filters_by_priority_area_and_thread(ed25519_server):
    h = _auth(ed25519_server, "tenant_A")
    run_id = _new_run_id()
    _seed_run("tenant_A", run_id)
    repo.save_review(_triage_review(run_id))

    def ids(query: str) -> list[str]:
        resp = client.get(f"/v1/reviews/{run_id}/issues{query}", headers=h)
        assert resp.status_code == 200, resp.text
        return sorted(i["id"].rsplit(":", 1)[1] for i in resp.json())

    with TestClient(app) as client:
        assert ids("") == ["I001", "I002", "I003", "I004", "I005"]
        assert ids("?priority=blocker") == ["I001"]
        assert ids("?priority=should-fix") == ["I002", "I003"]
        assert ids("?priority=needs-human") == ["I004"]
        assert ids("?priority=nit") == ["I005"]
        assert ids("?area=accuracy") == ["I003"]
        assert ids("?area=delivery") == ["I004"]
        assert ids("?area=sync") == ["I001", "I002", "I005"]
        assert ids("?area=video") == []
        assert ids("?thread_id=T001") == ["I001", "I002"]
        assert ids("?thread_id=T999") == []
        assert ids("?thread_id=T001&priority=should-fix") == ["I002"]
        assert ids("?area=sync&severity=minor") == ["I005"]  # combines with the existing filters
        assert client.get(f"/v1/reviews/{run_id}/issues?priority=urgent", headers=h).status_code == 422
        assert client.get(f"/v1/reviews/{run_id}/issues?area=nope", headers=h).status_code == 422

        body = {i["id"].rsplit(":", 1)[1]: i["body"] for i in client.get(f"/v1/reviews/{run_id}/issues", headers=h).json()}
    assert body["I001"]["priority"] == "blocker" and body["I001"]["thread_id"] == "T001"
    assert body["I001"]["area"] == "sync" and body["I001"]["tags"] == ["area:sync", "on-camera"]
    assert body["I003"]["tags"] == ["area:accuracy", "llm"]
    assert body["I004"]["priority"] == "needs-human"


def test_issue_row_body_persists_triage_fields_and_old_rows_are_backfilled(ed25519_server):
    h = _auth(ed25519_server, "tenant_A")
    new_run, old_run = _new_run_id(), _new_run_id()
    for run_id in (new_run, old_run):
        _seed_run("tenant_A", run_id)
    repo.save_review(_triage_review(new_run))
    repo.save_review(_triage_review(old_run, with_triage=False))  # issue bodies saved without labels

    with get_sessionmaker()() as db:
        rows = {r.id: r.body for r in repo.get_issues(db, "tenant_A", new_run)}
        assert rows["dubbing:hi->es:I001"]["priority"] == "blocker"
        assert rows["dubbing:hi->es:I001"]["thread_id"] == "T001"
        old_rows = repo.get_issues(db, "tenant_A", old_run)
        assert all(r.body.get("priority") is None for r in old_rows)

    with TestClient(app) as client:
        resp = client.get(f"/v1/reviews/{old_run}/issues?priority=blocker", headers=h)
        assert [i["id"].rsplit(":", 1)[1] for i in resp.json()] == ["I001"]
        assert resp.json()[0]["body"]["thread_id"] == "T001" and resp.json()[0]["body"]["area"] == "sync"
        resp = client.get(f"/v1/reviews/{old_run}/issues?thread_id=T001", headers=h)
        assert len(resp.json()) == 2


def test_issues_filters_stay_tenant_scoped(ed25519_server):
    run_id = _new_run_id()
    _seed_run("tenant_A", run_id)
    repo.save_review(_triage_review(run_id))
    with TestClient(app) as client:
        resp = client.get(f"/v1/reviews/{run_id}/issues?priority=blocker", headers=_auth(ed25519_server, "tenant_B"))
    assert resp.status_code == 404


# --- POST /uploads validation -----------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename, role, content_type",
    [
        ("dub.mp4", "candidate", "video/mp4"),
        ("dub.mp4", "candidate", None),  # no content type: the extension decides
        ("dub.mkv", "candidate", ""),  # browsers leave it empty for some containers
        ("dub.mp4", "candidate", "video/mp4; codecs=avc1.64001f"),
        ("DUB.MOV", "original", "video/quicktime"),
        ("track.wav", "candidate", "audio/wav"),
        ("track.m4a", "original", "audio/mp4"),
        ("ref.png", "reference", "image/png"),
        ("ref.jpg", "reference", "image/jpeg"),
        ("ref.jpeg", "reference", "image/jpeg"),
        ("ref.webp", "reference", "image/webp"),
        ("ref.mp4", "reference", "video/mp4"),  # references may also be video or audio
        ("ref.png", "reference", None),
    ],
)
def test_upload_accepts_valid_media(ed25519_server, filename, role, content_type):
    with TestClient(app) as client:
        resp = _upload(client, _auth(ed25519_server, "tenant_A"), filename, role, content_type)
    assert resp.status_code == 201, resp.text


@pytest.mark.parametrize(
    "filename, role, content_type",
    [
        ("ref.png", "candidate", "image/png"),  # images are reference-only
        ("ref.png", "original", "image/png"),
        ("ref.png", "candidate", None),
        ("dub.mp4", "candidate", "image/png"),
        ("ref.png", "reference", "video/mp4"),  # content type and extension disagree
        ("dub.mp4", "reference", "image/png"),
        ("ref.gif", "reference", "image/gif"),
        ("ref.png", "reference", "image/gif"),
        ("ref.svg", "reference", "image/svg+xml"),
        ("notes.txt", "candidate", "text/plain"),
        ("notes.txt", "candidate", None),
        ("dub.mp4", "candidate", "application/pdf"),
        ("dub.mp4", "candidate", "application/octet-stream"),
        ("dub.exe", "candidate", "video/mp4"),
        ("dub", "candidate", "video/mp4"),
        ("dub.mp4.sh", "candidate", None),
    ],
)
def test_upload_rejects_unsupported_media_with_415(ed25519_server, filename, role, content_type):
    with TestClient(app) as client:
        resp = _upload(client, _auth(ed25519_server, "tenant_A"), filename, role, content_type)
    assert resp.status_code == 415, resp.text
    assert resp.json()["detail"]


@pytest.mark.parametrize("filename", ["../dub.mp4", "a/b.mp4", "..\\dub.mp4", "..", ""])
def test_upload_rejects_path_like_filenames(ed25519_server, filename):
    with TestClient(app) as client:
        resp = _upload(client, _auth(ed25519_server, "tenant_A"), filename, "candidate", "video/mp4")
    assert resp.status_code == 400


# --- reference images and GET /reviews/{run_id}/media/{asset_id} ------------------------------------------------

def test_reference_is_attached_to_the_run_and_playable_before_the_review_finishes(ed25519_server):
    h = _auth(ed25519_server, "tenant_M")
    with TestClient(app) as client:
        ids = {}
        for name, role, ctype in [("source.mp4", "original", "video/mp4"), ("dub.mp4", "candidate", "video/mp4"),
                                  ("look.png", "reference", "image/png")]:
            resp = _upload(client, h, name, role, ctype)
            assert resp.status_code == 201
            ids[role] = resp.json()["asset_id"]
        stray = _upload(client, h, "stray.mp3", "candidate", "audio/mpeg").json()["asset_id"]  # not in the review

        with patch("vreview.api.services.enqueue_review_workflow", new_callable=AsyncMock) as enqueue:
            enqueue.return_value = "hatchet_x"
            resp = client.post(
                "/v1/reviews",
                json={"assets": [{"asset_id": ids["original"], "role": "original"},
                                 {"asset_id": ids["reference"], "role": "reference"},
                                 {"asset_id": ids["candidate"], "role": "candidate"}],
                      "brief": {"kind": "dub", "source_language": "hi", "target_languages": ["es"]}},
                headers=h,
            )
        assert resp.status_code == 202
        run_id = resp.json()["run_id"]
        review_input = enqueue.call_args.args[0]
        assert [(m.role, m.filename) for m in review_input.media] == [
            ("original", "source.mp4"), ("reference", "look.png"), ("candidate", "dub.mp4")]

        item = next(i for i in client.get("/v1/reviews", headers=h).json()["items"] if i["run_id"] == run_id)
        assert item["filenames"] == ["dub.mp4", "source.mp4", "look.png"]
        assert item["status"] == "queued" and item["verdict"] is None and item["counts"] is None

        ref = client.get(f"/v1/reviews/{run_id}/media/{ids['reference']}", headers=h)
        assert ref.status_code == 200
        assert set(ref.json()) == {"url", "role", "filename", "content_type", "kind"}
        assert ref.json()["role"] == "reference" and ref.json()["kind"] == "image"
        assert ref.json()["filename"] == "look.png" and ref.json()["content_type"] == "image/png"
        assert ref.json()["url"].endswith("look.png")  # not ingested yet: the upload object

        # an asset the tenant owns but did not attach to this run
        assert client.get(f"/v1/reviews/{run_id}/media/{stray}", headers=h).status_code == 404


def test_media_endpoint_serves_ingested_media_with_kind_and_content_type(ed25519_server):
    h = _auth(ed25519_server, "tenant_A")
    run_id = _new_run_id()
    media = [
        MediaRef(asset_id="ast_cand", role="candidate", key="media/" + "c" * 64 + ".mp4", sha256="c" * 64, filename="dub.mp4"),
        MediaRef(asset_id="ast_orig", role="original", key="media/" + "a" * 64 + ".wav", sha256="a" * 64, filename="source.wav"),
        MediaRef(asset_id="ast_ref", role="reference", key="media/" + "r" * 64 + ".webp", sha256="r" * 64, filename="ref.webp"),
    ]
    _seed_run("tenant_A", run_id, media=[{"asset_id": m.asset_id, "role": m.role, "filename": m.filename} for m in media])
    repo.save_review(_triage_review(run_id, media))

    with TestClient(app) as client:
        got = {m.asset_id: client.get(f"/v1/reviews/{run_id}/media/{m.asset_id}", headers=h) for m in media}
        assert all(r.status_code == 200 for r in got.values())
        cand, orig, ref = (got[m.asset_id].json() for m in media)
        assert (cand["kind"], cand["content_type"], cand["role"], cand["filename"]) == ("video", "video/mp4", "candidate", "dub.mp4")
        assert (orig["kind"], orig["content_type"]) == ("audio", "audio/wav")
        assert (ref["kind"], ref["content_type"], ref["role"]) == ("image", "image/webp", "reference")
        assert cand["url"].endswith(media[0].key)  # the ingested, content-addressed object

        assert client.get(f"/v1/reviews/{run_id}/media/ast_other", headers=h).status_code == 404
        assert client.get(f"/v1/reviews/run_missing/media/ast_cand", headers=h).status_code == 404


def test_media_endpoint_is_tenant_scoped(ed25519_server):
    run_id = _new_run_id()
    media = [MediaRef(asset_id="ast_cand", role="candidate", key="media/x.mp4", sha256="x", filename="dub.mp4")]
    _seed_run("tenant_A", run_id, media=[{"asset_id": "ast_cand", "role": "candidate", "filename": "dub.mp4"}])
    repo.save_review(_triage_review(run_id, media))
    with TestClient(app) as client:
        own = client.get(f"/v1/reviews/{run_id}/media/ast_cand", headers=_auth(ed25519_server, "tenant_A"))
        other = client.get(f"/v1/reviews/{run_id}/media/ast_cand", headers=_auth(ed25519_server, "tenant_B"))
        anonymous = client.get(f"/v1/reviews/{run_id}/media/ast_cand")
    assert (own.status_code, other.status_code, anonymous.status_code) == (200, 404, 401)


# --- artifact name validation -----------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["..", "../secret.json", "a/../../b.json", "dubbing/..%2f..%2fx", "a..b.json",
                                  "/etc/passwd", "\\windows\\x"])
def test_artifact_name_with_dotdot_or_leading_slash_is_rejected(ed25519_server, name):
    from vreview.api import services

    run_id = _new_run_id()
    _seed_run("tenant_A", run_id)
    # the service check is the guarantee (HTTP clients may normalise ".." out of the URL before it is sent)
    with pytest.raises(services.HTTPException) as exc:
        services.get_artifact_url("tenant_A", run_id, name)
    assert exc.value.status_code == 400


def test_artifact_name_rejected_over_http(ed25519_server):
    h = _auth(ed25519_server, "tenant_A")
    run_id = _new_run_id()
    _seed_run("tenant_A", run_id)
    with TestClient(app) as client:
        for path in (f"/v1/reviews/{run_id}/artifacts/%2e%2e%2fsecret.json",  # decoded to "../secret.json"
                     f"/v1/reviews/{run_id}/artifacts//etc/passwd",  # name = "/etc/passwd"
                     f"/v1/reviews/{run_id}/artifacts/a%2F..%2F..%2Fb"):
            resp = client.get(path, headers=h)
            assert resp.status_code == 400, (path, resp.status_code, resp.text)
        ok = client.get(f"/v1/reviews/{run_id}/artifacts/dubbing/hi->es.json", headers=h)
        assert ok.status_code == 200 and "url" in ok.json()


# --- CORS -------------------------------------------------------------------------------------------------------

def test_cors_allows_the_web_app_origin_by_default(ed25519_server):
    web = "http://localhost:53000"
    with TestClient(app) as client:
        pre = client.options(
            "/v1/reviews",
            headers={"Origin": web, "Access-Control-Request-Method": "GET",
                     "Access-Control-Request-Headers": "authorization,content-type"},
        )
        assert pre.status_code == 200
        assert pre.headers["access-control-allow-origin"] == web
        assert "authorization" in pre.headers["access-control-allow-headers"].lower()
        assert "access-control-allow-credentials" not in pre.headers

        simple = client.get("/v1/reviews", headers={**_auth(ed25519_server, "tenant_A"), "Origin": web})
        assert simple.status_code == 200
        assert simple.headers["access-control-allow-origin"] == web

        # a 401 must carry the header too, or the browser reports a CORS error instead of "unauthorized"
        denied = client.get("/v1/reviews", headers={"Origin": web})
        assert denied.status_code == 401 and denied.headers["access-control-allow-origin"] == web

        evil = client.options(
            "/v1/reviews", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
        assert "access-control-allow-origin" not in evil.headers


def test_cors_origins_setting_is_comma_separated(monkeypatch):
    assert Settings().cors_origins == ["http://localhost:53000"]
    monkeypatch.setenv("VREVIEW_CORS_ORIGINS", "https://app.example.com, http://localhost:53000 ,")
    assert Settings().cors_origins == ["https://app.example.com", "http://localhost:53000"]


def test_triage_llm_models_setting_is_comma_separated_and_empty_by_default(monkeypatch):
    monkeypatch.delenv("VREVIEW_TRIAGE_LLM_MODELS", raising=False)
    assert Settings(_env_file=None).triage_llm_models == []
    monkeypatch.setenv("VREVIEW_TRIAGE_LLM_MODELS", "gemini-flash, auto/best-fast")
    assert Settings(_env_file=None).triage_llm_models == ["gemini-flash", "auto/best-fast"]
    monkeypatch.setenv("VREVIEW_TRIAGE_LLM_MODELS", "")
    assert Settings(_env_file=None).triage_llm_models == []


# --- POST /uploads/{asset_id}/abort -----------------------------------------------------------------------------

def _start_upload(client, headers, filename: str = "cand.mp4", size: int = 20 * 1024 * 1024) -> dict:
    """Creates an upload and writes a chunk to each presigned part URL (LocalStore file:// URLs)."""
    resp = client.post(
        "/v1/uploads",
        json={"filename": filename, "size": size, "role": "candidate", "content_type": "video/mp4"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    data["part_paths"] = [Path(p["url"].replace("file://", "")) for p in data["parts"]]
    for path in data["part_paths"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"chunk")
    data["complete_body"] = {
        "upload_id": data["upload_id"],
        "parts": [{"PartNumber": p["part_number"], "ETag": f"etag_{p['part_number']}"} for p in data["parts"]],
    }
    return data


def _asset_status(asset_id: str) -> str:
    with get_sessionmaker()() as db:
        return db.get(repo.MediaAssetRow, asset_id).status


def test_abort_upload_aborts_in_the_store_and_marks_the_asset(ed25519_server):
    from vreview.storage import get_store

    h = _auth(ed25519_server, "tenant_A")
    with TestClient(app) as client:
        up = _start_upload(client, h)
        assert len(up["parts"]) == 2 and all(p.exists() for p in up["part_paths"])
        assert _asset_status(up["asset_id"]) == "uploading"

        resp = client.post(f"/v1/uploads/{up['asset_id']}/abort", headers=h)
        assert resp.status_code == 200
        assert resp.json() == {"asset_id": up["asset_id"], "status": "aborted"}

        assert not any(p.exists() for p in up["part_paths"])  # the store dropped the uploaded parts
        assert not get_store().exists(up["upload_key"])
        assert _asset_status(up["asset_id"]) == "aborted"


def test_complete_is_rejected_after_abort(ed25519_server):
    from vreview.storage import get_store

    h = _auth(ed25519_server, "tenant_A")
    with TestClient(app) as client:
        up = _start_upload(client, h)
        assert client.post(f"/v1/uploads/{up['asset_id']}/abort", headers=h).status_code == 200

        resp = client.post(f"/v1/uploads/{up['asset_id']}/complete", json=up["complete_body"], headers=h)
        assert resp.status_code == 409
        assert "aborted" in resp.json()["detail"].lower()
        assert not get_store().exists(up["upload_key"])
        assert _asset_status(up["asset_id"]) == "aborted"


def test_create_review_rejects_aborted_asset(ed25519_server):
    h = _auth(ed25519_server, "tenant_A")
    with TestClient(app) as client:
        up = _start_upload(client, h)
        assert client.post(f"/v1/uploads/{up['asset_id']}/abort", headers=h).status_code == 200
        resp = client.post("/v1/reviews", json={"assets": [{"asset_id": up["asset_id"], "role": "candidate"}],
                                                 "brief": {"kind": "dub"}}, headers=h)
        assert resp.status_code == 409
        assert "aborted" in resp.json()["detail"].lower()


def test_abort_is_idempotent(ed25519_server):
    h = _auth(ed25519_server, "tenant_A")
    with TestClient(app) as client:
        up = _start_upload(client, h)
        first = client.post(f"/v1/uploads/{up['asset_id']}/abort", headers=h)
        second = client.post(f"/v1/uploads/{up['asset_id']}/abort", headers=h)
        assert first.status_code == second.status_code == 200
        assert second.json() == {"asset_id": up["asset_id"], "status": "aborted"}


def test_abort_after_complete_is_409_and_keeps_the_object(ed25519_server):
    from vreview.storage import get_store

    h = _auth(ed25519_server, "tenant_A")
    with TestClient(app) as client:
        up = _start_upload(client, h)
        done = client.post(f"/v1/uploads/{up['asset_id']}/complete", json=up["complete_body"], headers=h)
        assert done.status_code == 200
        assert _asset_status(up["asset_id"]) == "completed"

        resp = client.post(f"/v1/uploads/{up['asset_id']}/abort", headers=h)
        assert resp.status_code == 409
        assert "completed" in resp.json()["detail"].lower()
        assert get_store().exists(up["upload_key"])
        assert _asset_status(up["asset_id"]) == "completed"


def test_abort_is_tenant_scoped(ed25519_server):
    h_a = _auth(ed25519_server, "tenant_A")
    h_b = _auth(ed25519_server, "tenant_B")
    with TestClient(app) as client:
        up = _start_upload(client, h_a)

        assert client.post(f"/v1/uploads/{up['asset_id']}/abort", headers=h_b).status_code == 404
        assert client.post("/v1/uploads/ast_does_not_exist/abort", headers=h_a).status_code == 404
        assert client.post(f"/v1/uploads/{up['asset_id']}/abort").status_code == 401

        # tenant B's attempt changed nothing: the owner can still finish the upload
        assert _asset_status(up["asset_id"]) == "uploading"
        assert all(p.exists() for p in up["part_paths"])
        done = client.post(f"/v1/uploads/{up['asset_id']}/complete", json=up["complete_body"], headers=h_a)
        assert done.status_code == 200


def test_abort_leaves_the_asset_uploading_when_the_store_fails(ed25519_server):
    h = _auth(ed25519_server, "tenant_A")
    with TestClient(app) as client:
        up = _start_upload(client, h)
        with patch("vreview.storage.local.LocalStore.abort_multipart", side_effect=OSError("store down")):
            resp = client.post(f"/v1/uploads/{up['asset_id']}/abort", headers=h)
        assert resp.status_code == 500
        assert _asset_status(up["asset_id"]) == "uploading"

        retry = client.post(f"/v1/uploads/{up['asset_id']}/abort", headers=h)
        assert retry.status_code == 200 and _asset_status(up["asset_id"]) == "aborted"


# --- GET /reviews/{run_id}/feedback -----------------------------------------------------------------------------

def _post_feedback(client, headers, run_id: str, issue_id: str, verdict: str, comment: str | None = None) -> None:
    body = {"verdict": verdict} if comment is None else {"verdict": verdict, "comment": comment}
    resp = client.post(f"/v1/reviews/{run_id}/issues/{issue_id}/feedback", json=body, headers=headers)
    assert resp.status_code == 200, resp.text


def _seed_review(tenant: str) -> str:
    run_id = _new_run_id()
    _seed_run(tenant, run_id)
    repo.save_review(_triage_review(run_id))
    return run_id


I1, I2, I3 = ("dubbing:hi->es:I001", "dubbing:hi->es:I002", "dubbing:hi->es:I003")


def test_feedback_list_returns_every_row_oldest_first(ed25519_server):
    h = _auth(ed25519_server, "tenant_A")
    run_id = _seed_review("tenant_A")
    with TestClient(app) as client:
        _post_feedback(client, h, run_id, I1, "accept", "real bug")
        _post_feedback(client, h, run_id, I2, "comment")
        _post_feedback(client, h, run_id, I1, "false_positive", "changed my mind")
        _post_feedback(client, h, run_id, I3, "accept")

        resp = client.get(f"/v1/reviews/{run_id}/feedback", headers=h)

    assert resp.status_code == 200
    rows = resp.json()
    assert [(r["issue_id"], r["verdict"], r["comment"]) for r in rows] == [
        (I1, "accept", "real bug"),
        (I2, "comment", None),
        (I1, "false_positive", "changed my mind"),
        (I3, "accept", None),
    ]
    assert all(set(r) == {"issue_id", "verdict", "comment", "created_at"} for r in rows)
    stamps = [datetime.fromisoformat(r["created_at"]) for r in rows]
    assert stamps == sorted(stamps) and len(set(stamps)) == 4


def test_feedback_list_latest_keeps_one_row_per_issue(ed25519_server):
    h = _auth(ed25519_server, "tenant_A")
    run_id = _seed_review("tenant_A")
    with TestClient(app) as client:
        _post_feedback(client, h, run_id, I1, "accept", "real bug")
        _post_feedback(client, h, run_id, I2, "comment", "hmm")
        _post_feedback(client, h, run_id, I1, "false_positive", "changed my mind")
        _post_feedback(client, h, run_id, I1, "comment", "latest word")

        latest = client.get(f"/v1/reviews/{run_id}/feedback?latest=true", headers=h)
        explicit_false = client.get(f"/v1/reviews/{run_id}/feedback?latest=false", headers=h)
        everything = client.get(f"/v1/reviews/{run_id}/feedback", headers=h)
        bad = client.get(f"/v1/reviews/{run_id}/feedback?latest=maybe", headers=h)

    assert latest.status_code == 200
    assert [(r["issue_id"], r["verdict"], r["comment"]) for r in latest.json()] == [
        (I2, "comment", "hmm"),
        (I1, "comment", "latest word"),
    ]
    assert len(everything.json()) == 4 and explicit_false.json() == everything.json()
    assert bad.status_code == 422


def test_feedback_list_is_empty_for_a_run_without_feedback_and_ignores_other_runs(ed25519_server):
    h = _auth(ed25519_server, "tenant_A")
    quiet, busy = _seed_review("tenant_A"), _seed_review("tenant_A")
    with TestClient(app) as client:
        _post_feedback(client, h, busy, I1, "accept")
        assert client.get(f"/v1/reviews/{quiet}/feedback", headers=h).json() == []
        assert client.get(f"/v1/reviews/{quiet}/feedback?latest=true", headers=h).json() == []
        assert len(client.get(f"/v1/reviews/{busy}/feedback", headers=h).json()) == 1


def test_feedback_list_is_tenant_scoped(ed25519_server):
    h_a = _auth(ed25519_server, "tenant_A")
    h_b = _auth(ed25519_server, "tenant_B")
    run_id = _seed_review("tenant_A")
    with TestClient(app) as client:
        _post_feedback(client, h_a, run_id, I1, "accept")

        for suffix in ("", "?latest=true"):
            assert client.get(f"/v1/reviews/{run_id}/feedback{suffix}", headers=h_b).status_code == 404
        assert client.get("/v1/reviews/run_missing/feedback", headers=h_a).status_code == 404
        assert client.get(f"/v1/reviews/{run_id}/feedback").status_code == 401
        assert len(client.get(f"/v1/reviews/{run_id}/feedback", headers=h_a).json()) == 1


# --- auth defaults ----------------------------------------------------------------------------------------------

AUTH_ENV_VARS = ("VREVIEW_AUTH_JWKS_URL", "VREVIEW_AUTH_ISSUER", "VREVIEW_AUTH_AUDIENCE")


def test_auth_settings_default_to_the_web_app_on_53000(monkeypatch):
    for var in AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    cfg = Settings(_env_file=None)
    assert cfg.auth_jwks_url == "http://localhost:53000/api/auth/jwks"
    assert cfg.auth_issuer == "http://localhost:53000"
    assert cfg.auth_audience == "http://localhost:53000"


def test_auth_settings_can_still_be_overridden_or_switched_off(monkeypatch):
    monkeypatch.setenv("VREVIEW_AUTH_JWKS_URL", "https://auth.example.com/api/auth/jwks")
    monkeypatch.setenv("VREVIEW_AUTH_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("VREVIEW_AUTH_AUDIENCE", "")
    cfg = Settings(_env_file=None)
    assert cfg.auth_jwks_url == "https://auth.example.com/api/auth/jwks"
    assert cfg.auth_issuer == "https://auth.example.com"
    assert not cfg.auth_audience


def test_env_example_auth_values_match_the_defaults(monkeypatch):
    for var in AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    cfg = Settings(_env_file=None)
    lines = (Path(__file__).resolve().parents[1] / ".env.example").read_text().splitlines()
    example = dict(line.split("=", 1) for line in lines if line.startswith("VREVIEW_AUTH_"))
    assert example == {
        "VREVIEW_AUTH_JWKS_URL": cfg.auth_jwks_url,
        "VREVIEW_AUTH_ISSUER": cfg.auth_issuer,
        "VREVIEW_AUTH_AUDIENCE": cfg.auth_audience,
    }
