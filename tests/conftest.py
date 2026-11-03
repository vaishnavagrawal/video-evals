"""Shared test setup."""
from __future__ import annotations

import pytest

from vreview import webhook_guard
from vreview.config import settings


@pytest.fixture(autouse=True)
def _auth_claims_not_checked_unless_a_test_asks(monkeypatch):
    """The default issuer/audience are the web app's URL, and a local .env may set its own. Test tokens carry no
    iss/aud, so switch the checks off here; tests that exercise them set the variables themselves.

    The webhook and upload limits are pinned to their defaults for the same reason."""
    monkeypatch.setenv("VREVIEW_AUTH_ISSUER", "")
    monkeypatch.setenv("VREVIEW_AUTH_AUDIENCE", "")
    monkeypatch.setenv("VREVIEW_WEBHOOK_ALLOW_HTTP", "0")
    monkeypatch.setenv("VREVIEW_MAX_UPLOAD_BYTES", str(2 * 1024**3))
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def allow_loopback_webhooks(monkeypatch):
    """For tests that deliver a webhook to a receiver on 127.0.0.1: allows http (VREVIEW_WEBHOOK_ALLOW_HTTP=1) and
    counts IPv4 loopback as public. Every other private range stays blocked."""
    monkeypatch.setenv("VREVIEW_WEBHOOK_ALLOW_HTTP", "1")
    settings.cache_clear()
    real = webhook_guard.is_public_address
    monkeypatch.setattr(
        webhook_guard,
        "is_public_address",
        lambda ip: (ip.version == 4 and ip.is_loopback) or real(ip),
    )
