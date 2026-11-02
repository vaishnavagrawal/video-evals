"""Blob store contract and key layout.

Keys (bucket-relative):
  uploads/<upload_id>/<filename>                          raw client uploads (not trusted, not hashed)
  media/<sha256><ext>                                     content-addressed media
  cache/<sha256>/<stage>@<version>/<config_hash>/<name>   stage outputs; same inputs -> same key
  reviews/<run_id>/<name>                                 per-run artifacts (frames, report.json)
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol


def media_key(sha256: str, ext: str) -> str:
    return f"media/{sha256}{ext.lower()}"


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode()).hexdigest()[:16]


def cache_prefix(sha256: str, stage: str, version: str, cfg: dict) -> str:
    return f"cache/{sha256}/{stage}@{version}/{config_hash(cfg)}"


def review_key(run_id: str, name: str) -> str:
    return f"reviews/{run_id}/{name}"


def sha256_file(path: Path, chunk: int = 8 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


class BlobStore(Protocol):
    def exists(self, key: str) -> bool: ...
    def get_file(self, key: str, dest: Path) -> Path:
        """Download to `dest` (parents created). Skips the download if `dest` exists with the same size."""
    def put_file(self, key: str, src: Path, content_type: str | None = None) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None: ...
    def copy(self, src_key: str, dst_key: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def presign_get(self, key: str, ttl: int | None = None) -> str: ...

    # Browser uploads (S3 multipart). part_size is chosen by the caller.
    def create_multipart(self, key: str, content_type: str | None = None) -> str:
        """Returns the store's upload id."""
    def presign_part(self, key: str, upload_id: str, part_number: int, ttl: int | None = None) -> str: ...
    def complete_multipart(self, key: str, upload_id: str, parts: list[dict]) -> None:
        """parts: [{"PartNumber": int, "ETag": str}] as returned by the browser."""
    def abort_multipart(self, key: str, upload_id: str) -> None: ...
