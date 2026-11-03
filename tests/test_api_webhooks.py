"""Tests for webhook delivery and HMAC-SHA256 signature verification."""
from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import threading
import uuid
from typing import Generator
from unittest.mock import patch

import psycopg
import pytest
from sqlalchemy import create_engine

from vreview.config import settings
from vreview.db import repo
from vreview.db.models import Base
from vreview.db.session import get_engine, get_sessionmaker
from vreview.webhooks import deliver

COMPOSE_PG_HOST = "localhost"
COMPOSE_PG_PORT = 55433


@pytest.fixture(scope="session")
def webhook_test_db():
    try:
        conn = psycopg.connect(
            f"postgresql://vreview:vreview@{COMPOSE_PG_HOST}:{COMPOSE_PG_PORT}/vreview",
            autocommit=True,
        )
    except Exception as exc:
        pytest.skip(f"Compose Postgres is unreachable: {exc}")

    test_dbname = f"test_webhook_{uuid.uuid4().hex[:8]}"
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


@pytest.fixture
def webhook_receiver():
    """Starts an ephemeral HTTP server to receive webhook POSTs."""
    received = []

    class ReceiverHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            received.append({
                "headers": dict(self.headers),
                "body": body,
                "json": json.loads(body.decode("utf-8")),
            })
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), ReceiverHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield {
        "url": f"http://127.0.0.1:{port}/webhook",
        "received": received,
    }

    server.shutdown()
    server.server_close()


def test_webhook_delivery_and_signature_verification(
    webhook_test_db, webhook_receiver, allow_loopback_webhooks, monkeypatch
):
    secret = "my_top_secret_key"
    monkeypatch.setenv("VREVIEW_DATABASE_URL", webhook_test_db)
    monkeypatch.setenv("VREVIEW_WEBHOOK_SECRET", secret)
    settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()

    run_id = "run_wh_123"
    with get_sessionmaker()() as db:
        repo.create_run(
            db=db,
            tenant="tenant_wh",
            run_id=run_id,
            brief={"kind": "dub"},
            webhook_url=webhook_receiver["url"],
            status="succeeded",
        )

    # Deliver webhook
    deliver(run_id)

    assert len(webhook_receiver["received"]) == 1
    call = webhook_receiver["received"][0]

    # Verify JSON content
    data = call["json"]
    assert data["run_id"] == run_id
    assert data["status"] == "succeeded"
    assert "/v1/reviews/run_wh_123" in data["review_url"]

    # Verify HMAC-SHA256 signature
    sig_header = call["headers"].get("x-vreview-signature") or call["headers"].get("X-VReview-Signature")
    assert sig_header is not None
    assert sig_header.startswith("sha256=")
    received_sig = sig_header.split("sha256=")[1]

    expected_sig = hmac.new(
        secret.encode("utf-8"),
        call["body"],
        hashlib.sha256,
    ).hexdigest()

    assert hmac.compare_digest(received_sig, expected_sig)


def test_webhook_retries_and_never_raises(webhook_test_db, allow_loopback_webhooks, monkeypatch):
    monkeypatch.setenv("VREVIEW_DATABASE_URL", webhook_test_db)
    monkeypatch.setenv("VREVIEW_WEBHOOK_SECRET", "secret")
    settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()

    run_id = "run_wh_fail"
    with get_sessionmaker()() as db:
        repo.create_run(
            db=db,
            tenant="tenant_wh",
            run_id=run_id,
            brief={"kind": "dub"},
            webhook_url="http://127.0.0.1:59999/nonexistent",
            status="failed",
        )

    with patch("time.sleep") as mock_sleep:
        # deliver must never raise
        deliver(run_id)
        # Should have attempted 3 times (with 2 sleep intervals)
        assert mock_sleep.call_count == 2


def test_webhook_to_a_private_address_is_not_sent_and_not_retried(webhook_test_db, monkeypatch):
    """A URL stored before the guard existed (or whose DNS changed since) is refused at delivery time."""
    monkeypatch.setenv("VREVIEW_DATABASE_URL", webhook_test_db)
    monkeypatch.setenv("VREVIEW_WEBHOOK_SECRET", "secret")
    settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()

    run_id = "run_wh_private"
    with get_sessionmaker()() as db:
        repo.create_run(
            db=db,
            tenant="tenant_wh",
            run_id=run_id,
            brief={"kind": "dub"},
            webhook_url="https://169.254.169.254/latest/meta-data",
            status="succeeded",
        )

    with patch("time.sleep") as mock_sleep, patch("vreview.webhooks.httpx.Client") as mock_client:
        deliver(run_id)  # never raises

    mock_client.assert_not_called()
    mock_sleep.assert_not_called()


def test_webhook_redirect_is_not_followed_or_retried(webhook_test_db, allow_loopback_webhooks, monkeypatch):
    monkeypatch.setenv("VREVIEW_DATABASE_URL", webhook_test_db)
    settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()

    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/internal")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        run_id = "run_wh_redirect"
        with get_sessionmaker()() as db:
            repo.create_run(
                db=db,
                tenant="tenant_wh",
                run_id=run_id,
                brief={"kind": "dub"},
                webhook_url=f"http://127.0.0.1:{server.server_address[1]}/hook",
                status="succeeded",
            )
        with patch("time.sleep") as mock_sleep:
            deliver(run_id)
    finally:
        server.shutdown()
        server.server_close()

    assert hits == ["/hook"]
    mock_sleep.assert_not_called()
