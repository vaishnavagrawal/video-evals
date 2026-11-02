"""JWT Authentication for vreview API using better-auth JWKS."""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Header, HTTPException, status
from jwt import PyJWKClient, PyJWKClientError, PyJWTError

from vreview.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def _get_jwk_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, cache_keys=True)


def get_jwk_client(jwks_url: str | None = None) -> PyJWKClient:
    url = jwks_url or settings().auth_jwks_url
    return _get_jwk_client(url)


def verify_jwt(token: str, jwks_url: str | None = None) -> tuple[str, dict[str, Any]]:
    """Verifies a JWT token against JWKS. Returns (tenant_id, claims).

    Raises HTTPException(401) if invalid, expired, or missing 'sub'.
    """
    try:
        client = get_jwk_client(jwks_url)
        signing_key = client.get_signing_key_from_jwt(token)

        alg = signing_key.algorithm_name or getattr(signing_key, "_jwk_data", {}).get("alg")
        algorithms = [alg] if alg else ["EdDSA", "RS256", "ES256", "PS256"]

        cfg = settings()
        kwargs: dict[str, Any] = {}
        options: dict[str, Any] = {}

        if cfg.auth_issuer:
            kwargs["issuer"] = cfg.auth_issuer
        else:
            options["verify_iss"] = False

        if cfg.auth_audience:
            kwargs["audience"] = cfg.auth_audience
        else:
            options["verify_aud"] = False

        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=algorithms,
            options=options,
            **kwargs,
        )

        sub = payload.get("sub")
        if not sub or not isinstance(sub, str):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="JWT token missing valid 'sub' claim",
            )
        return sub, payload

    except HTTPException:
        raise
    except (PyJWKClientError, PyJWTError) as exc:
        logger.warning("JWT verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication token: {exc}",
        ) from exc
    except Exception as exc:
        logger.error("Unexpected error verifying JWT: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
        ) from exc


def get_current_tenant(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str:
    """FastAPI dependency to extract and verify the current tenant from Bearer token."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Expected 'Bearer <token>'",
        )

    token = parts[1]
    tenant, _ = verify_jwt(token)
    return tenant
