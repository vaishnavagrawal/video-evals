from __future__ import annotations

import shutil
import uuid
from pathlib import Path


class LocalStore:
    """Filesystem-backed BlobStore for local development and unit tests."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._uploads_dir = self.root / ".multipart_uploads"

    def _path(self, key: str) -> Path:
        return self.root / key

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def get_file(self, key: str, dest: Path) -> Path:
        src = self._path(key)
        if not src.is_file():
            raise FileNotFoundError(f"Key not found in store: {key}")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size == src.stat().st_size:
            return dest
        shutil.copy2(src, dest)
        return dest

    def put_file(self, key: str, src: Path, content_type: str | None = None) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def get_bytes(self, key: str) -> bytes:
        src = self._path(key)
        if not src.is_file():
            raise FileNotFoundError(f"Key not found in store: {key}")
        return src.read_bytes()

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    def copy(self, src_key: str, dst_key: str) -> None:
        src = self._path(src_key)
        if not src.is_file():
            raise FileNotFoundError(f"Key not found in store: {src_key}")
        dest = self._path(dst_key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.is_file():
            p.unlink()

    def presign_get(self, key: str, ttl: int | None = None) -> str:
        return self._path(key).as_uri()

    def create_multipart(self, key: str, content_type: str | None = None) -> str:
        upload_id = uuid.uuid4().hex
        upload_dir = self._uploads_dir / upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        return upload_id

    def presign_part(self, key: str, upload_id: str, part_number: int, ttl: int | None = None) -> str:
        part_path = self._uploads_dir / upload_id / f"part_{part_number}"
        part_path.parent.mkdir(parents=True, exist_ok=True)
        return part_path.as_uri()

    def complete_multipart(self, key: str, upload_id: str, parts: list[dict]) -> None:
        upload_dir = self._uploads_dir / upload_id
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)

        sorted_parts = sorted(parts, key=lambda p: int(p["PartNumber"]))
        with dest.open("wb") as out:
            for part in sorted_parts:
                part_num = part["PartNumber"]
                part_file = upload_dir / f"part_{part_num}"
                if not part_file.is_file():
                    raise FileNotFoundError(f"Missing part {part_num} for upload {upload_id}")
                with part_file.open("rb") as pf:
                    shutil.copyfileobj(pf, out)

        shutil.rmtree(upload_dir, ignore_errors=True)

    def abort_multipart(self, key: str, upload_id: str) -> None:
        upload_dir = self._uploads_dir / upload_id
        shutil.rmtree(upload_dir, ignore_errors=True)
