from __future__ import annotations

import urllib.parse
import uuid
from pathlib import Path

import httpx

from vreview.jobs import MediaInput
from vreview.schema import MediaRef
from vreview.storage.base import BlobStore, media_key, sha256_file

MAX_INGEST_SIZE_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB


def _determine_extension(item: MediaInput) -> str:
    ext = ""
    if item.filename:
        ext = Path(item.filename).suffix
    elif item.url:
        ext = Path(urllib.parse.urlsplit(item.url).path).suffix
    elif item.key:
        ext = Path(item.key).suffix

    ext = ext.strip().lower()
    if not ext:
        return ".mp4"
    if not ext.startswith("."):
        return f".{ext}"
    return ext


def ingest(store: BlobStore, item: MediaInput, scratch: Path) -> MediaRef:
    """Ingest a media asset into content-addressed blob storage.

    If item.sha256 is set and the media key exists, re-ingest is skipped.
    Otherwise obtains bytes from store or via https streaming with a 4 GiB cap,
    computes SHA-256, puts to media/<sha256><ext> (skip if exists),
    cleans up scratch, and returns MediaRef.
    """
    ext = _determine_extension(item)

    if item.sha256:
        existing_key = media_key(item.sha256, ext)
        if store.exists(existing_key):
            return MediaRef(
                asset_id=item.asset_id,
                role=item.role,
                key=existing_key,
                sha256=item.sha256,
                filename=item.filename,
            )

    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    temp_path = scratch / f"ingest_{item.asset_id}_{uuid.uuid4().hex}{ext}"

    try:
        if item.key:
            store.get_file(item.key, temp_path)
        elif item.url:
            parsed = urllib.parse.urlsplit(item.url)
            if parsed.scheme.lower() != "https":
                raise ValueError(f"Only HTTPS URLs are allowed for ingest, got '{item.url}'")

            with httpx.Client(follow_redirects=True, timeout=60.0) as client:
                with client.stream("GET", item.url) as response:
                    response.raise_for_status()
                    cl = response.headers.get("content-length")
                    if cl and int(cl) > MAX_INGEST_SIZE_BYTES:
                        raise ValueError(
                            f"Remote file size ({cl} bytes) exceeds {MAX_INGEST_SIZE_BYTES} bytes limit"
                        )
                    downloaded = 0
                    with temp_path.open("wb") as out:
                        for chunk in response.iter_bytes(chunk_size=1 << 20):
                            downloaded += len(chunk)
                            if downloaded > MAX_INGEST_SIZE_BYTES:
                                raise ValueError(
                                    f"Download exceeded {MAX_INGEST_SIZE_BYTES} bytes limit"
                                )
                            out.write(chunk)
        else:
            raise ValueError("MediaInput must specify either key or url")

        sha = sha256_file(temp_path)
        target_key = media_key(sha, ext)

        if not store.exists(target_key):
            store.put_file(target_key, temp_path)

        return MediaRef(
            asset_id=item.asset_id,
            role=item.role,
            key=target_key,
            sha256=sha,
            filename=item.filename,
        )
    finally:
        if temp_path.exists():
            temp_path.unlink()
