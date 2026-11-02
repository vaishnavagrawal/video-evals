"""Process settings, read from VREVIEW_* environment variables (and .env).

Hatchet reads its own HATCHET_CLIENT_* variables; LLM credentials keep the dubeval DUBEVAL_LLM_* names.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VREVIEW_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://vreview:vreview@localhost:5432/vreview"

    # Blob storage. "r2" talks the S3 API: Cloudflare R2 in production, MinIO in local compose.
    store: Literal["r2", "local"] = "r2"
    local_store_root: Path = Path("var/blobs")
    r2_endpoint_url: str = "http://localhost:9000"        # https://<account_id>.r2.cloudflarestorage.com
    r2_public_endpoint_url: str | None = None              # host used in presigned URLs, if it differs
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "vreview"
    presign_ttl_seconds: int = 900

    scratch_dir: Path = Path("/tmp/vreview")               # worker-local downloads, never authoritative

    # better-auth issues JWTs; we verify them against its JWKS endpoint.
    auth_jwks_url: str = "http://localhost:3000/api/auth/jwks"
    auth_issuer: str | None = None
    auth_audience: str | None = None

    webhook_secret: str = ""                               # HMAC-SHA256 key for outgoing webhooks
    public_base_url: str = "http://localhost:8000"


@lru_cache
def settings() -> Settings:
    return Settings()
