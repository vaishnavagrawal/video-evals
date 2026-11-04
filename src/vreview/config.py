"""Process settings, read from VREVIEW_* environment variables (and .env).

Hatchet reads its own HATCHET_CLIENT_* variables; LLM credentials keep the dubeval DUBEVAL_LLM_* names.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    max_upload_bytes: int = 2 * 1024**3                    # POST /v1/uploads rejects larger files with 413

    scratch_dir: Path = Path("/tmp/vreview")               # worker-local downloads, never authoritative

    # better-auth (hosted by the web app on :53000) issues JWTs; we verify them against its JWKS endpoint.
    # better-auth sets iss and aud to its base URL. Set VREVIEW_AUTH_ISSUER / VREVIEW_AUTH_AUDIENCE to an
    # empty string to skip that check.
    auth_jwks_url: str = "http://localhost:53000/api/auth/jwks"
    auth_issuer: str | None = "http://localhost:53000"
    auth_audience: str | None = "http://localhost:53000"

    # Browser origins allowed to call the API (comma-separated in VREVIEW_CORS_ORIGINS).
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:53000"]

    # Model roles. Each is a comma-separated fallback list of model references, `provider:model` or a bare
    # model for the default provider (see dubeval.providers), e.g. `meta:muse-spark-1.3,gateway:gemini-flash`.
    # A brief can still override them per review through `options`.
    # Text judge for dubbing meaning/tone (VREVIEW_JUDGE_MODELS). Empty = DubConfig's built-in default.
    judge_models: Annotated[list[str], NoDecode] = []
    # Writes review-thread comments and the summary (VREVIEW_TRIAGE_LLM_MODELS). Empty = rule-based text only.
    triage_llm_models: Annotated[list[str], NoDecode] = []
    # Multimodal (video + audio) second-opinion reviewer (VREVIEW_AV_JUDGE_MODELS). Empty = evaluator skipped.
    av_judge_models: Annotated[list[str], NoDecode] = []
    # Cloud ASR model (VREVIEW_ASR_MODEL). Empty = DubConfig's built-in default; "none" = local Whisper only.
    asr_model: str | None = None

    webhook_secret: str = ""                               # HMAC-SHA256 key for outgoing webhooks
    # Webhook URLs must be https and resolve to public addresses (vreview.webhook_guard). Set
    # VREVIEW_WEBHOOK_ALLOW_HTTP=1 to also accept http:// (local development and tests only).
    webhook_allow_http: bool = False
    public_base_url: str = "http://localhost:8000"

    @field_validator("cors_origins", "judge_models", "triage_llm_models", "av_judge_models", mode="before")
    @classmethod
    def _split_csv(cls, value):
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value


@lru_cache
def settings() -> Settings:
    return Settings()
