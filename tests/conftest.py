"""Shared test setup."""
from __future__ import annotations

import pytest

from vreview.config import settings


@pytest.fixture(autouse=True)
def _auth_claims_not_checked_unless_a_test_asks(monkeypatch):
    """The default issuer/audience are the web app's URL, and a local .env may set its own. Test tokens carry no
    iss/aud, so switch the checks off here; tests that exercise them set the variables themselves."""
    monkeypatch.setenv("VREVIEW_AUTH_ISSUER", "")
    monkeypatch.setenv("VREVIEW_AUTH_AUDIENCE", "")
    settings.cache_clear()
    yield
    settings.cache_clear()
