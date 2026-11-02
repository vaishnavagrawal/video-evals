from __future__ import annotations

import hashlib
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

import httpx
import pytest

from vreview.ingest import ingest
from vreview.jobs import MediaInput
from vreview.storage.base import media_key, sha256_file
from vreview.storage.local import LocalStore
from vreview.storage.r2 import R2Store

MINIO_ENDPOINT = "http://localhost:59000"
MINIO_ACCESS_KEY = "minioadmin"
MINIO_SECRET_KEY = "minioadmin"
MINIO_BUCKET = "vreview"


def is_minio_reachable() -> bool:
    try:
        r = httpx.get(f"{MINIO_ENDPOINT}/minio/health/live", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def test_local_store_roundtrip(tmp_path: Path):
    store = LocalStore(tmp_path / "blobs")
    key = "test/file.txt"

    assert not store.exists(key)
    with pytest.raises(FileNotFoundError):
        store.get_bytes(key)

    data = b"Hello from local store!"
    store.put_bytes(key, data, content_type="text/plain")
    assert store.exists(key)
    assert store.get_bytes(key) == data

    # Test get_file
    dest1 = tmp_path / "downloads" / "f1.txt"
    out_path = store.get_file(key, dest1)
    assert out_path == dest1
    assert dest1.read_bytes() == data

    # Test get_file skip when file exists with same size
    mtime_before = dest1.stat().st_mtime
    store.get_file(key, dest1)
    assert dest1.stat().st_mtime == mtime_before

    # Test put_file
    src2 = tmp_path / "src2.bin"
    src2.write_bytes(b"Binary payload 12345")
    key2 = "test/binary.bin"
    store.put_file(key2, src2)
    assert store.get_bytes(key2) == b"Binary payload 12345"

    # Test copy
    copy_key = "test/copy.bin"
    store.copy(key2, copy_key)
    assert store.exists(copy_key)
    assert store.get_bytes(copy_key) == b"Binary payload 12345"

    # Test delete
    store.delete(key)
    assert not store.exists(key)

    # Test presign_get returns file:// URI
    presigned = store.presign_get(copy_key)
    assert presigned.startswith("file://")
    parsed_path = Path(urllib.request.url2pathname(urllib.parse.urlparse(presigned).path))
    assert parsed_path.exists()
    assert parsed_path.read_bytes() == b"Binary payload 12345"


def test_local_store_multipart_emulation(tmp_path: Path):
    store = LocalStore(tmp_path / "blobs")
    key = "uploads/multipart.bin"

    upload_id = store.create_multipart(key)
    assert upload_id

    p1_url = store.presign_part(key, upload_id, 1)
    p2_url = store.presign_part(key, upload_id, 2)
    assert p1_url.startswith("file://")
    assert p2_url.startswith("file://")

    p1_path = Path(urllib.request.url2pathname(urllib.parse.urlparse(p1_url).path))
    p2_path = Path(urllib.request.url2pathname(urllib.parse.urlparse(p2_url).path))

    part1_bytes = b"PART_1_DATA_"
    part2_bytes = b"PART_2_DATA"
    p1_path.write_bytes(part1_bytes)
    p2_path.write_bytes(part2_bytes)

    store.complete_multipart(
        key,
        upload_id,
        [
            {"PartNumber": 2, "ETag": "dummy-etag-2"},
            {"PartNumber": 1, "ETag": "dummy-etag-1"},
        ],
    )

    assert store.exists(key)
    assert store.get_bytes(key) == part1_bytes + part2_bytes

    # Test abort multipart
    abort_upload_id = store.create_multipart("uploads/aborted.bin")
    abort_url = store.presign_part("uploads/aborted.bin", abort_upload_id, 1)
    abort_path = Path(urllib.request.url2pathname(urllib.parse.urlparse(abort_url).path))
    abort_path.write_bytes(b"temp data")
    assert abort_path.exists()

    store.abort_multipart("uploads/aborted.bin", abort_upload_id)
    assert not abort_path.exists()


@pytest.mark.skipif(not is_minio_reachable(), reason="MinIO is not reachable")
def test_r2_store_roundtrip(tmp_path: Path):
    store = R2Store(
        endpoint_url=MINIO_ENDPOINT,
        access_key_id=MINIO_ACCESS_KEY,
        secret_access_key=MINIO_SECRET_KEY,
        bucket=MINIO_BUCKET,
    )

    key = "test/r2_roundtrip.txt"
    content = b"Testing R2 / MinIO roundtrip"

    store.put_bytes(key, content, content_type="text/plain")
    assert store.exists(key)
    assert store.get_bytes(key) == content

    # get_file and size-check skip
    dest = tmp_path / "r2_down.txt"
    store.get_file(key, dest)
    assert dest.read_bytes() == content

    mtime_before = dest.stat().st_mtime
    store.get_file(key, dest)
    assert dest.stat().st_mtime == mtime_before

    # copy
    copy_key = "test/r2_copy.txt"
    store.copy(key, copy_key)
    assert store.exists(copy_key)
    assert store.get_bytes(copy_key) == content

    # delete
    store.delete(key)
    store.delete(copy_key)
    assert not store.exists(key)
    assert not store.exists(copy_key)


@pytest.mark.skipif(not is_minio_reachable(), reason="MinIO is not reachable")
def test_r2_store_presign_get_httpx():
    store = R2Store(
        endpoint_url=MINIO_ENDPOINT,
        access_key_id=MINIO_ACCESS_KEY,
        secret_access_key=MINIO_SECRET_KEY,
        bucket=MINIO_BUCKET,
        public_endpoint_url=MINIO_ENDPOINT,
    )

    key = "test/presign_fetch.txt"
    payload = b"Presigned URL fetchable payload!"
    store.put_bytes(key, payload)

    url = store.presign_get(key, ttl=300)
    assert url.startswith("http")

    resp = httpx.get(url, timeout=10.0)
    assert resp.status_code == 200
    assert resp.content == payload

    store.delete(key)


@pytest.mark.skipif(not is_minio_reachable(), reason="MinIO is not reachable")
def test_r2_store_multipart_minio():
    store = R2Store(
        endpoint_url=MINIO_ENDPOINT,
        access_key_id=MINIO_ACCESS_KEY,
        secret_access_key=MINIO_SECRET_KEY,
        bucket=MINIO_BUCKET,
        public_endpoint_url=MINIO_ENDPOINT,
    )

    key = "test/multipart_minio.bin"
    upload_id = store.create_multipart(key)
    assert upload_id

    # S3 requires minimum 5 MiB for all parts except the last one
    part1_bytes = b"X" * (5 * 1024 * 1024)
    part2_bytes = b"Y" * (1 * 1024 * 1024)

    url1 = store.presign_part(key, upload_id, 1)
    resp1 = httpx.put(url1, content=part1_bytes, timeout=30.0)
    assert resp1.status_code == 200
    etag1 = resp1.headers.get("etag", "").strip('"')
    assert etag1

    url2 = store.presign_part(key, upload_id, 2)
    resp2 = httpx.put(url2, content=part2_bytes, timeout=30.0)
    assert resp2.status_code == 200
    etag2 = resp2.headers.get("etag", "").strip('"')
    assert etag2

    store.complete_multipart(
        key,
        upload_id,
        [
            {"PartNumber": 1, "ETag": etag1},
            {"PartNumber": 2, "ETag": etag2},
        ],
    )

    assert store.exists(key)
    combined = store.get_bytes(key)
    assert len(combined) == 6 * 1024 * 1024
    assert combined == (part1_bytes + part2_bytes)

    store.delete(key)

    # Test abort multipart
    abort_id = store.create_multipart("test/multipart_abort.bin")
    store.abort_multipart("test/multipart_abort.bin", abort_id)


def test_ingest_content_addressing(tmp_path: Path):
    store = LocalStore(tmp_path / "store")
    scratch = tmp_path / "scratch"

    content = b"Video content for test ingest 12345"
    sha = hashlib.sha256(content).hexdigest()
    expected_key = media_key(sha, ".mp4")

    # Put raw upload in uploads/
    raw_key = "uploads/item1/sample.mp4"
    store.put_bytes(raw_key, content)

    item1 = MediaInput(
        asset_id="asset_1",
        role="original",
        key=raw_key,
        filename="sample.mp4",
    )

    ref1 = ingest(store, item1, scratch)
    assert ref1.asset_id == "asset_1"
    assert ref1.role == "original"
    assert ref1.key == expected_key
    assert ref1.sha256 == sha
    assert ref1.filename == "sample.mp4"
    assert store.exists(expected_key)
    assert store.get_bytes(expected_key) == content

    # Second ingest with same content: same key, doesn't re-upload
    raw_key2 = "uploads/item2/sample2.mp4"
    store.put_bytes(raw_key2, content)
    item2 = MediaInput(
        asset_id="asset_2",
        role="candidate",
        key=raw_key2,
        filename="sample2.mp4",
    )

    ref2 = ingest(store, item2, scratch)
    assert ref2.key == ref1.key
    assert ref2.sha256 == ref1.sha256

    # Test fast-path when sha256 already set and key exists in store
    item3 = MediaInput(
        asset_id="asset_3",
        role="reference",
        sha256=sha,
        filename="fast.mp4",
    )
    ref3 = ingest(store, item3, scratch)
    assert ref3.key == expected_key
    assert ref3.sha256 == sha

    # Verify scratch was cleaned up
    assert list(scratch.glob("ingest_*")) == []


def test_ingest_https_only_rejection(tmp_path: Path):
    store = LocalStore(tmp_path / "store")
    scratch = tmp_path / "scratch"

    # Reject http://
    item_http = MediaInput(
        asset_id="asset_insecure",
        role="original",
        url="http://example.com/video.mp4",
    )
    with pytest.raises(ValueError, match="Only HTTPS URLs are allowed"):
        ingest(store, item_http, scratch)

    # Reject ftp://
    item_ftp = MediaInput(
        asset_id="asset_ftp",
        role="original",
        url="ftp://example.com/video.mp4",
    )
    with pytest.raises(ValueError, match="Only HTTPS URLs are allowed"):
        ingest(store, item_ftp, scratch)

    # Reject no key and no url
    item_empty = MediaInput(
        asset_id="asset_empty",
        role="original",
    )
    with pytest.raises(ValueError, match="must specify either key or url"):
        ingest(store, item_empty, scratch)
