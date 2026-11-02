"""Job inputs shared by the API (enqueue side) and Hatchet workflows (execute side)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from .schema import Brief, MediaRole


class MediaInput(BaseModel):
    """A media asset before ingest. Exactly one of `key` (uploaded blob) or `url` (remote https) is set."""
    asset_id: str
    role: MediaRole
    key: str | None = Field(default=None, description="uploads/<asset_id>/<filename> after multipart complete")
    url: str | None = None
    filename: str | None = None
    sha256: str | None = Field(default=None, description="Set when already ingested (re-review)")


class ReviewInput(BaseModel):
    run_id: str
    brief: Brief
    media: list[MediaInput]
    evaluators: list[str] | None = Field(default=None, description="None = every applicable evaluator")
