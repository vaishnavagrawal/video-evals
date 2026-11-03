"""Database repository functions (sync engine; workflow workers and API call these)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from vreview.db.models import (
    IssueFeedbackRow,
    IssueRow,
    MediaAssetRow,
    ProjectRow,
    ReviewRow,
    RunRow,
    VerdictEnum,
    gen_id,
    utcnow,
)
from vreview.db.session import get_sessionmaker
from vreview.schema import Review


def mark_run(
    run_id: str,
    status: Literal["queued", "running", "succeeded", "failed"],
    error: str | None = None,
    stage: str | None = None,
    db: Session | None = None,
) -> None:
    """Updates a run's status, error, and stage."""
    def _apply(session: Session) -> None:
        run = session.get(RunRow, run_id)
        if not run:
            return
        run.status = status
        if error is not None:
            run.error = error
        if stage is not None:
            run.stage = stage
        run.updated_at = utcnow()
        session.commit()

    if db is not None:
        _apply(db)
    else:
        with get_sessionmaker()() as session:
            _apply(session)


def save_review(review: Review, db: Session | None = None) -> None:
    """Upserts reviews row (jsonb) + replaces issues rows; sets run succeeded."""
    def _apply(session: Session) -> None:
        review_data = review.model_dump(mode="json")
        review_row = session.get(ReviewRow, review.run_id)
        if review_row:
            review_row.score = review.score
            review_row.body = review_data
            review_row.created_at = review.created_at
        else:
            review_row = ReviewRow(
                run_id=review.run_id,
                score=review.score,
                body=review_data,
                created_at=review.created_at,
            )
            session.add(review_row)

        # Replace issues rows for this run
        session.execute(delete(IssueRow).where(IssueRow.run_id == review.run_id))

        for result in review.results:
            for issue in result.issues:
                issue_row = IssueRow(
                    id=issue.id,
                    run_id=review.run_id,
                    evaluator=issue.evaluator,
                    category=issue.category,
                    severity=issue.severity,
                    confidence=issue.confidence,
                    t0=issue.t0,
                    t1=issue.t1,
                    track=issue.track,
                    description=issue.description,
                    fix_hint=issue.fix_hint,
                    body=issue.model_dump(mode="json"),
                )
                session.add(issue_row)

        # Set run succeeded
        run = session.get(RunRow, review.run_id)
        if run:
            run.status = "succeeded"
            run.stage = "completed"
            run.error = None
            run.updated_at = utcnow()

        session.commit()

    if db is not None:
        _apply(db)
    else:
        with get_sessionmaker()() as session:
            _apply(session)


# --- API helper functions ---

def create_media_asset(
    db: Session,
    tenant: str,
    filename: str,
    size: int,
    upload_key: str,
    upload_id: str | None = None,
    role: str = "candidate",
    project_id: str | None = None,
    asset_id: str | None = None,
) -> MediaAssetRow:
    asset = MediaAssetRow(
        id=asset_id or gen_id("ast"),
        tenant=tenant,
        project_id=project_id,
        role=role,
        filename=filename,
        upload_key=upload_key,
        upload_id=upload_id,
        size=size,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def get_media_asset(db: Session, tenant: str, asset_id: str, for_update: bool = False) -> MediaAssetRow | None:
    """for_update locks the row until the session commits or closes, so complete and abort cannot interleave."""
    stmt = select(MediaAssetRow).where(
        MediaAssetRow.id == asset_id,
        MediaAssetRow.tenant == tenant,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return db.scalars(stmt).first()


def set_media_asset_status(db: Session, asset: MediaAssetRow, status: str) -> None:
    asset.status = status
    db.commit()


def create_run(
    db: Session,
    tenant: str,
    run_id: str,
    brief: dict[str, Any],
    evaluators: list[str] | None = None,
    webhook_url: str | None = None,
    project_id: str | None = None,
    status: str = "queued",
    media: list[dict[str, Any]] | None = None,
) -> RunRow:
    run = RunRow(
        id=run_id,
        tenant=tenant,
        project_id=project_id,
        status=status,
        webhook_url=webhook_url,
        brief=brief,
        evaluators=evaluators,
        media=media,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def update_run_hatchet_id(db: Session, run_id: str, hatchet_run_id: str) -> None:
    run = db.get(RunRow, run_id)
    if run:
        run.hatchet_run_id = hatchet_run_id
        run.updated_at = utcnow()
        db.commit()


def get_run(db: Session, tenant: str, run_id: str) -> RunRow | None:
    stmt = select(RunRow).where(
        RunRow.id == run_id,
        RunRow.tenant == tenant,
    )
    return db.scalars(stmt).first()


def get_run_by_id(db: Session, run_id: str) -> RunRow | None:
    """Get run without tenant filter (for internal/webhook callers)."""
    return db.get(RunRow, run_id)


def list_runs(
    db: Session,
    tenant: str,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[RunRow], int]:
    count_stmt = select(func.count()).select_from(RunRow).where(RunRow.tenant == tenant)
    total = db.scalar(count_stmt) or 0

    stmt = (
        select(RunRow)
        .where(RunRow.tenant == tenant)
        .order_by(RunRow.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = list(db.scalars(stmt).all())
    return items, total


def get_review(db: Session, tenant: str, run_id: str) -> ReviewRow | None:
    stmt = (
        select(ReviewRow)
        .join(RunRow, ReviewRow.run_id == RunRow.id)
        .where(
            ReviewRow.run_id == run_id,
            RunRow.tenant == tenant,
        )
    )
    return db.scalars(stmt).first()


def get_issues(
    db: Session,
    tenant: str,
    run_id: str,
    severity: str | None = None,
    category: str | None = None,
    evaluator: str | None = None,
) -> list[IssueRow]:
    # Ensure run belongs to tenant
    run = get_run(db, tenant, run_id)
    if not run:
        return []

    stmt = select(IssueRow).where(IssueRow.run_id == run_id)
    if severity:
        stmt = stmt.where(IssueRow.severity == severity)
    if category:
        stmt = stmt.where(IssueRow.category == category)
    if evaluator:
        stmt = stmt.where(IssueRow.evaluator == evaluator)

    stmt = stmt.order_by(IssueRow.severity, IssueRow.t0)
    return list(db.scalars(stmt).all())


def create_issue_feedback(
    db: Session,
    tenant: str,
    run_id: str,
    issue_id: str,
    verdict: VerdictEnum,
    comment: str | None = None,
) -> IssueFeedbackRow:
    feedback = IssueFeedbackRow(
        id=gen_id("fb"),
        run_id=run_id,
        issue_id=issue_id,
        tenant=tenant,
        verdict=verdict,
        comment=comment,
    )
    db.add(feedback)
    db.commit()
    db.refresh(feedback)
    return feedback


def list_issue_feedback(db: Session, tenant: str, run_id: str) -> list[IssueFeedbackRow]:
    """All feedback rows for a run, oldest first."""
    stmt = (
        select(IssueFeedbackRow)
        .where(IssueFeedbackRow.tenant == tenant, IssueFeedbackRow.run_id == run_id)
        .order_by(IssueFeedbackRow.created_at, IssueFeedbackRow.id)
    )
    return list(db.scalars(stmt).all())
