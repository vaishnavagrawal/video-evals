"""Tests for FastMCP tools and auth mounted at /mcp."""
from __future__ import annotations

import http.server
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine

from vreview.api.app import app
from vreview.config import settings
from vreview.db import repo
from vreview.db.models import Base
from vreview.db.session import get_engine, get_sessionmaker
from vreview.schema import Brief, EvaluatorResult, Issue, Review

COMPOSE_PG_HOST = "localhost"
COMPOSE_PG_PORT = 55433


@pytest.fixture(scope="session")
def mcp_test_db():
    try:
        conn = psycopg.connect(
            f"postgresql://vreview:vreview@{COMPOSE_PG_HOST}:{COMPOSE_PG_PORT}/vreview",
            autocommit=True,
        )
    except Exception as exc:
        pytest.skip(f"Compose Postgres is unreachable: {exc}")

    test_dbname = f"test_mcp_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE {test_dbname};")
    conn.close()

    db_url = f"postgresql+psycopg://vreview:vreview@{COMPOSE_PG_HOST}:{COMPOSE_PG_PORT}/{test_dbname}"
    engine = create_engine(db_url)
    Base.metadata.create_all(bind=engine)

    yield db_url

    engine.dispose()
    try:
        conn = psycopg.connect(
            f"postgresql://vreview:vreview@{COMPOSE_PG_HOST}:{COMPOSE_PG_PORT}/vreview",
            autocommit=True,
        )
        with conn.cursor() as cur:
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
def mcp_ed25519_server():
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    alg = jwt.algorithms.get_default_algorithms()["EdDSA"]
    jwk_dict = alg.to_jwk(public_key, as_dict=True)
    jwk_dict["kid"] = "mcp-test-key"
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
        "kid": "mcp-test-key",
        "url": f"http://127.0.0.1:{port}/.well-known/jwks.json",
    }

    server.shutdown()
    server.server_close()


def generate_mcp_token(server_info, tenant: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": tenant,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=2)).timestamp()),
    }
    return jwt.encode(
        payload,
        server_info["private_key"],
        algorithm="EdDSA",
        headers={"kid": server_info["kid"]},
    )

def parse_mcp_response(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(text)

def extract_tool_result(parsed_mcp: dict) -> Any:
    result_obj = parsed_mcp["result"]
    if "structuredContent" in result_obj:
        sc = result_obj["structuredContent"]
        if isinstance(sc, dict) and "result" in sc and len(sc) == 1:
            return sc["result"]
        return sc
    if "content" in result_obj and result_obj["content"]:
        return json.loads(result_obj["content"][0]["text"])
    return None

@pytest.fixture(autouse=True)
def setup_mcp_env(mcp_test_db, mcp_ed25519_server, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("VREVIEW_DATABASE_URL", mcp_test_db)
    monkeypatch.setenv("VREVIEW_AUTH_JWKS_URL", mcp_ed25519_server["url"])
    monkeypatch.setenv("VREVIEW_STORE", "local")
    monkeypatch.setenv("VREVIEW_LOCAL_STORE_ROOT", str(tmp_path / "blobs"))
    settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
    from vreview.storage import get_store
    get_store.cache_clear()
    from vreview.api.auth import _get_jwk_client
    _get_jwk_client.cache_clear()


@pytest.mark.anyio
async def test_mcp_auth_rejection(mcp_ed25519_server):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Missing auth
            res1 = await client.post("/mcp/")
            assert res1.status_code == 401

            # Invalid auth
            res2 = await client.post("/mcp/", headers={"Authorization": "Bearer invalid.jwt.here"})
            assert res2.status_code == 401


@pytest.mark.anyio
async def test_mcp_tools_execution_and_isolation(mcp_ed25519_server):
    token_a = generate_mcp_token(mcp_ed25519_server, "tenant_mcp_a")
    token_b = generate_mcp_token(mcp_ed25519_server, "tenant_mcp_b")
    asset_id = f"ast_mcp_{uuid.uuid4().hex[:8]}"
    # Seed an asset for tenant A
    with get_sessionmaker()() as db:
        repo.create_media_asset(
            db=db,
            tenant="tenant_mcp_a",
            filename="video.mp4",
            size=1024,
            upload_key=f"uploads/{asset_id}/video.mp4",
            asset_id=asset_id,
        )

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            with patch("vreview.api.services.enqueue_review_workflow", new_callable=AsyncMock) as mock_enqueue:
                mock_enqueue.return_value = "hatchet_mcp_run_999"

                # 1. review_video tool call by tenant A
                req_body = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "review_video",
                        "arguments": {
                            "asset_ids": [asset_id],
                            "roles": ["candidate"],
                            "brief": {"kind": "dub"},
                        },
                    },
                }
                res = await client.post(
                    "/mcp/",
                    json=req_body,
                    headers={"Authorization": f"Bearer {token_a}"},
                )
                assert res.status_code == 200
                res_json = parse_mcp_response(res.text)
                assert "result" in res_json
                struct_res = extract_tool_result(res_json)
                run_id = struct_res["run_id"]
                assert struct_res["status"] == "queued"
                assert struct_res["hatchet_run_id"] == "hatchet_mcp_run_999"

            # 2. get_review by tenant A
            get_req = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_review",
                    "arguments": {"run_id": run_id},
                },
            }
            res_get = await client.post(
                "/mcp/",
                json=get_req,
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert res_get.status_code == 200
            run_data = extract_tool_result(parse_mcp_response(res_get.text))
            assert run_data["run_id"] == run_id
            assert run_data["status"] == "queued"

            # 3. get_review by tenant B -> isolated (error/not found)
            res_b = await client.post(
                "/mcp/",
                json=get_req,
                headers={"Authorization": f"Bearer {token_b}"},
            )
            assert res_b.status_code == 200
            # MCP tool returns isError: true when HTTPException 404 is raised
            assert parse_mcp_response(res_b.text)["result"]["isError"] is True

            # 4. Save review results and query get_issues
            rev = Review(
                run_id=run_id,
                brief=Brief(kind="dub"),
                media=[],
                results=[
                    EvaluatorResult(
                        evaluator="dubbing",
                        version="1",
                        status="ok",
                        score=90.0,
                        issues=[
                            Issue(
                                id="issue_1",
                                evaluator="dubbing",
                                category="audio",
                                severity="critical",
                                confidence=0.9,
                                t0=1.0,
                                t1=2.0,
                                track="pair1",
                                description="Audio clipping",
                                fix_hint="Lower volume",
                            )
                        ],
                    )
                ],
                score=90.0,
                created_at=datetime.now(timezone.utc),
            )
            repo.save_review(rev)

            # Query get_issues
            issues_req = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_issues",
                    "arguments": {
                        "run_id": run_id,
                        "severity": "critical",
                    },
                },
            }
            res_issues = await client.post(
                "/mcp/",
                json=issues_req,
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert res_issues.status_code == 200
            issues_data = extract_tool_result(parse_mcp_response(res_issues.text))
            assert len(issues_data) == 1
            assert issues_data[0]["id"] == "issue_1"
            assert issues_data[0]["severity"] == "critical"
