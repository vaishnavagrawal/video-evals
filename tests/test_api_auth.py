"""Tests for API authentication: Ed25519 JWKS, expiration, audience, issuer."""
from __future__ import annotations

import http.server
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Generator

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from vreview.api.app import app
from vreview.config import Settings, settings


@pytest.fixture(scope="module")
def ed25519_auth_server() -> Generator[dict, None, None]:
    """Generates an Ed25519 keypair and starts a local HTTP server serving JWKS."""
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    alg = jwt.algorithms.get_default_algorithms()["EdDSA"]
    jwk_dict = alg.to_jwk(public_key, as_dict=True)
    jwk_dict["kid"] = "test-ed25519-key"
    jwk_dict["alg"] = "EdDSA"
    jwk_dict["use"] = "sig"

    jwks_bytes = json.dumps({"keys": [jwk_dict]}).encode("utf-8")

    class JWKSHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(jwks_bytes)

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), JWKSHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    jwks_url = f"http://127.0.0.1:{port}/.well-known/jwks.json"

    yield {
        "private_key": private_key,
        "public_key": public_key,
        "kid": "test-ed25519-key",
        "jwks_url": jwks_url,
    }

    server.shutdown()
    server.server_close()


def make_token(
    auth_ctx: dict,
    sub: str = "tenant_test",
    iss: str = "https://auth.example.com",
    aud: str = "vreview-api",
    exp_delta: timedelta = timedelta(hours=1),
    override_claims: dict | None = None,
    private_key=None,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "iss": iss,
        "aud": aud,
        "iat": int(now.timestamp()),
        "exp": int((now + exp_delta).timestamp()),
    }
    if override_claims:
        payload.update(override_claims)

    key = private_key or auth_ctx["private_key"]
    return jwt.encode(
        payload,
        key,
        algorithm="EdDSA",
        headers={"kid": auth_ctx["kid"]},
    )


def test_healthz_unauthenticated():
    with TestClient(app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_valid_token_passes(ed25519_auth_server, monkeypatch):
    monkeypatch.setenv("VREVIEW_AUTH_JWKS_URL", ed25519_auth_server["jwks_url"])
    monkeypatch.setenv("VREVIEW_AUTH_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("VREVIEW_AUTH_AUDIENCE", "vreview-api")
    settings.cache_clear()
    from vreview.api.auth import _get_jwk_client
    _get_jwk_client.cache_clear()

    token = make_token(ed25519_auth_server, sub="tenant_1")
    with TestClient(app) as client:
        # GET /v1/reviews requires authentication
        resp = client.get("/v1/reviews", headers={"Authorization": f"Bearer {token}"})
        # Passes auth (200 with list of reviews)
        assert resp.status_code == 200
        assert "items" in resp.json()


def test_missing_authorization_header():
    with TestClient(app) as client:
        resp = client.get("/v1/reviews")
        assert resp.status_code == 401
        assert "Missing Authorization header" in resp.json()["detail"]


def test_malformed_authorization_header():
    with TestClient(app) as client:
        resp = client.get("/v1/reviews", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert resp.status_code == 401
        assert "Invalid Authorization header" in resp.json()["detail"]


def test_bad_signature_fails(ed25519_auth_server, monkeypatch):
    monkeypatch.setenv("VREVIEW_AUTH_JWKS_URL", ed25519_auth_server["jwks_url"])
    settings.cache_clear()
    from vreview.api.auth import _get_jwk_client
    _get_jwk_client.cache_clear()

    other_private_key = ed25519.Ed25519PrivateKey.generate()
    bad_token = make_token(ed25519_auth_server, private_key=other_private_key)

    with TestClient(app) as client:
        resp = client.get("/v1/reviews", headers={"Authorization": f"Bearer {bad_token}"})
        assert resp.status_code == 401
        assert "Invalid authentication token" in resp.json()["detail"]


def test_expired_token_fails(ed25519_auth_server, monkeypatch):
    monkeypatch.setenv("VREVIEW_AUTH_JWKS_URL", ed25519_auth_server["jwks_url"])
    settings.cache_clear()
    from vreview.api.auth import _get_jwk_client
    _get_jwk_client.cache_clear()

    expired_token = make_token(ed25519_auth_server, exp_delta=timedelta(hours=-1))
    with TestClient(app) as client:
        resp = client.get("/v1/reviews", headers={"Authorization": f"Bearer {expired_token}"})
        assert resp.status_code == 401
        assert "Signature has expired" in resp.json()["detail"]


def test_wrong_audience_fails(ed25519_auth_server, monkeypatch):
    monkeypatch.setenv("VREVIEW_AUTH_JWKS_URL", ed25519_auth_server["jwks_url"])
    monkeypatch.setenv("VREVIEW_AUTH_AUDIENCE", "expected-audience")
    settings.cache_clear()
    from vreview.api.auth import _get_jwk_client
    _get_jwk_client.cache_clear()

    token = make_token(ed25519_auth_server, aud="wrong-audience")
    with TestClient(app) as client:
        resp = client.get("/v1/reviews", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert "Audience doesn't match" in resp.json()["detail"]


def test_missing_sub_claim_fails(ed25519_auth_server, monkeypatch):
    monkeypatch.setenv("VREVIEW_AUTH_JWKS_URL", ed25519_auth_server["jwks_url"])
    settings.cache_clear()
    from vreview.api.auth import _get_jwk_client
    _get_jwk_client.cache_clear()

    now = datetime.now(timezone.utc)
    payload = {
        "iss": "test",
        "aud": "test",
        "exp": int((now + timedelta(hours=1)).timestamp()),
    }
    token = jwt.encode(payload, ed25519_auth_server["private_key"], algorithm="EdDSA", headers={"kid": ed25519_auth_server["kid"]})
    with TestClient(app) as client:
        resp = client.get("/v1/reviews", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert "missing valid 'sub' claim" in resp.json()["detail"]


def test_default_issuer_and_audience_accept_a_better_auth_token(ed25519_auth_server, monkeypatch):
    """With nothing configured but the JWKS location, tokens must carry iss/aud = the web app's base URL."""
    for var in ("VREVIEW_AUTH_ISSUER", "VREVIEW_AUTH_AUDIENCE", "VREVIEW_AUTH_JWKS_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        "vreview.api.auth.settings",
        lambda: Settings(_env_file=None, auth_jwks_url=ed25519_auth_server["jwks_url"]),
    )
    from vreview.api.auth import _get_jwk_client
    _get_jwk_client.cache_clear()

    web = "http://localhost:53000"
    good = make_token(ed25519_auth_server, iss=web, aud=web)
    wrong_iss = make_token(ed25519_auth_server, iss="https://auth.example.com", aud=web)
    wrong_aud = make_token(ed25519_auth_server, iss=web, aud="vreview-api")

    with TestClient(app) as client:
        ok = client.get("/v1/reviews", headers={"Authorization": f"Bearer {good}"})
        assert ok.status_code == 200
        bad_iss = client.get("/v1/reviews", headers={"Authorization": f"Bearer {wrong_iss}"})
        assert bad_iss.status_code == 401 and "Invalid issuer" in bad_iss.json()["detail"]
        bad_aud = client.get("/v1/reviews", headers={"Authorization": f"Bearer {wrong_aud}"})
        assert bad_aud.status_code == 401 and "Audience doesn't match" in bad_aud.json()["detail"]
