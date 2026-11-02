"""Database models for vreview (SQLAlchemy 2.x typed ORM, psycopg 3 compatible)."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    JSON,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSON type variant that uses JSONB on PostgreSQL and generic JSON on SQLite
JSONType = JSONB().with_variant(JSON, "sqlite")


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def gen_id(prefix: str = "") -> str:
    uid = uuid.uuid4().hex
    return f"{prefix}_{uid[:12]}" if prefix else uid


class VerdictEnum(str, enum.Enum):
    accept = "accept"
    false_positive = "false_positive"
    comment = "comment"


class ProjectRow(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: gen_id("prj"))
    tenant: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_projects_tenant_created_at", "tenant", "created_at"),
    )


class MediaAssetRow(Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: gen_id("ast"))
    tenant: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    role: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    upload_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    upload_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    media_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_media_assets_tenant_created_at", "tenant", "created_at"),
    )


class RunRow(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: gen_id("run"))
    tenant: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    hatchet_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    brief: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    evaluators: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    review: Mapped[ReviewRow | None] = relationship("ReviewRow", back_populates="run", uselist=False, cascade="all, delete-orphan")
    issues: Mapped[list[IssueRow]] = relationship("IssueRow", back_populates="run", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_runs_tenant_created_at", "tenant", "created_at"),
    )


class ReviewRow(Base):
    __tablename__ = "reviews"

    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    body: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped[RunRow] = relationship("RunRow", back_populates="review")


class IssueRow(Base):
    __tablename__ = "issues"

    id: Mapped[str] = mapped_column(String(256), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    evaluator: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    t0: Mapped[float] = mapped_column(Float, nullable=False)
    t1: Mapped[float] = mapped_column(Float, nullable=False)
    track: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    fix_hint: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)

    run: Mapped[RunRow] = relationship("RunRow", back_populates="issues")

    __table_args__ = (
        PrimaryKeyConstraint("id", "run_id"),
        Index("ix_issues_run_id_severity_category", "run_id", "severity", "category"),
    )


class IssueFeedbackRow(Base):
    __tablename__ = "issue_feedback"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: gen_id("fb"))
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    issue_id: Mapped[str] = mapped_column(String(256), nullable=False)
    tenant: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    verdict: Mapped[VerdictEnum] = mapped_column(
        Enum(VerdictEnum, name="verdict_enum", create_constraint=True), nullable=False
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_issue_feedback_tenant_created_at", "tenant", "created_at"),
        Index("ix_issue_feedback_run_id_issue_id", "run_id", "issue_id"),
    )
