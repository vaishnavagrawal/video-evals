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
