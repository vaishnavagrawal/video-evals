from __future__ import annotations

from functools import lru_cache

from vreview.config import settings
from vreview.storage.base import (
    BlobStore,
    cache_prefix,
    config_hash,
    media_key,
    review_key,
    sha256_file,
)
from vreview.storage.local import LocalStore
from vreview.storage.r2 import R2Store


@lru_cache
def get_store() -> BlobStore:
    s = settings()
    if s.store == "local":
        return LocalStore(s.local_store_root)
    return R2Store(
        endpoint_url=s.r2_endpoint_url,
        access_key_id=s.r2_access_key_id,
        secret_access_key=s.r2_secret_access_key,
        bucket=s.r2_bucket,
        public_endpoint_url=s.r2_public_endpoint_url,
        presign_ttl=s.presign_ttl_seconds,
    )


__all__ = [
    "BlobStore",
    "LocalStore",
    "R2Store",
    "get_store",
    "media_key",
    "config_hash",
    "cache_prefix",
    "review_key",
    "sha256_file",
]
