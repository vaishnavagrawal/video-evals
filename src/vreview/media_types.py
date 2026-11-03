"""Accepted upload types. One table drives upload validation and the media endpoint's `content_type`/`kind`."""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

MediaKind = Literal["video", "audio", "image"]

_EXTENSIONS: dict[str, tuple[str, MediaKind]] = {
    ".mp4": ("video/mp4", "video"),
    ".m4v": ("video/x-m4v", "video"),
    ".mov": ("video/quicktime", "video"),
    ".mkv": ("video/x-matroska", "video"),
    ".webm": ("video/webm", "video"),
    ".avi": ("video/x-msvideo", "video"),
    ".mpg": ("video/mpeg", "video"),
    ".mpeg": ("video/mpeg", "video"),
    ".ts": ("video/mp2t", "video"),
    ".mts": ("video/mp2t", "video"),
    ".m2ts": ("video/mp2t", "video"),
    ".mp3": ("audio/mpeg", "audio"),
    ".wav": ("audio/wav", "audio"),
    ".m4a": ("audio/mp4", "audio"),
    ".aac": ("audio/aac", "audio"),
    ".flac": ("audio/flac", "audio"),
    ".ogg": ("audio/ogg", "audio"),
    ".opus": ("audio/ogg", "audio"),
    ".png": ("image/png", "image"),
    ".jpg": ("image/jpeg", "image"),
    ".jpeg": ("image/jpeg", "image"),
    ".webp": ("image/webp", "image"),
}
_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


class UnsupportedMedia(ValueError):
    """The filename or content type is not accepted for this role."""


def _ext(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower()


def content_type_for(filename: str) -> str:
    return _EXTENSIONS.get(_ext(filename), ("application/octet-stream", "video"))[0]


def kind_for(filename: str) -> MediaKind:
    """Unknown extensions count as video (the main media type), so the player still gets a sane default."""
    return _EXTENSIONS.get(_ext(filename), ("", "video"))[1]


def _kind_of_content_type(content_type: str) -> MediaKind | None:
    if content_type in _IMAGE_TYPES:
        return "image"
    major = content_type.split("/", 1)[0]
    return major if major in ("video", "audio") else None  # type: ignore[return-value]


def validate_upload(filename: str, content_type: str | None, role: str) -> None:
    """Raise UnsupportedMedia unless `filename` and `content_type` are acceptable for `role`.

    `original` and `candidate` must be video or audio. `reference` may also be png/jpeg/webp.
    A missing content type is accepted (browsers leave it empty for some containers); the extension decides.
    """
    allowed: set[MediaKind] = {"video", "audio"} | ({"image"} if role == "reference" else set())

    ext_kind = _EXTENSIONS.get(_ext(filename), (None, None))[1]
    if ext_kind is None:
        raise UnsupportedMedia(f"Unsupported file extension {_ext(filename) or '(none)'!r} for {filename!r}")
    if ext_kind not in allowed:
        raise UnsupportedMedia(f"{role} media must be video or audio, got an {ext_kind} file ({filename!r})")

    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if not ct:
        return
    ct_kind = _kind_of_content_type(ct)
    if ct_kind is None or ct_kind not in allowed:
        raise UnsupportedMedia(f"Content type {ct!r} is not accepted for {role} media")
    if (ct_kind == "image") != (ext_kind == "image"):
        raise UnsupportedMedia(f"Content type {ct!r} does not match the file extension of {filename!r}")
