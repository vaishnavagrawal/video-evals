"""Core service functions for vreview API and MCP tools."""
from __future__ import annotations

import logging
import math
from typing import Any

from fastapi import HTTPException, status

from vreview.db.models import (
    ASSET_ABORTED,
    ASSET_COMPLETED,
    IssueFeedbackRow,
    IssueRow,
    MediaAssetRow,
    ReviewRow,
    RunRow,
    VerdictEnum,
    gen_id,
)
from vreview.db.repo import (
    create_issue_feedback as repo_create_issue_feedback,
    create_media_asset,
    create_run,
    get_issues as repo_get_issues,
    get_media_asset,
    get_review as repo_get_review,
    get_run,
    list_issue_feedback as repo_list_issue_feedback,
    list_runs as repo_list_runs,
    set_media_asset_status,
    update_run_hatchet_id,
)
from vreview.db.session import get_sessionmaker
from vreview.jobs import MediaInput, ReviewInput
from vreview.media_types import UnsupportedMedia, content_type_for, kind_for, validate_upload
from vreview.schema import Brief, MediaRole, Review
from vreview.storage import get_store, review_key
from vreview.triage import triage as run_triage

logger = logging.getLogger(__name__)

PART_SIZE = 16 * 1024 * 1024  # 16 MiB

_ROLE_ORDER = {"candidate": 0, "original": 1, "reference": 2}


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _with_triage(body: dict[str, Any]) -> dict[str, Any]:
    """Review body with a triage. Reviews stored before triage existed get the rule-only one, computed here
    and not written back. If the stored body no longer parses, it is returned untouched."""
    if body.get("triage"):
        return body
    try:
        review = Review.model_validate(body)
        review.triage = run_triage(review)
        return review.model_dump(mode="json")
    except Exception:
        logger.warning("Could not compute triage for stored review %s", body.get("run_id"), exc_info=True)
        return body


def _legacy_issue_fields(db, tenant: str, run_id: str) -> dict[str, dict[str, Any]]:
    """issue id -> triage fields, for issue rows stored before triage existed."""
    review_row = repo_get_review(db, tenant, run_id)
    if not review_row:
        return {}
    try:
        review = Review.model_validate(review_row.body)
        run_triage(review)
    except Exception:
        logger.warning("Could not compute triage for stored review %s", run_id, exc_info=True)
        return {}
    return {
        issue.id: {"priority": issue.priority, "tags": issue.tags, "area": issue.area, "thread_id": issue.thread_id}
        for result in review.results
        for issue in result.issues
    }


def _filenames(run: RunRow) -> list[str]:
    """Names of the files attached to a run, candidate first."""
    entries = run.media
    if entries is None:  # run created before runs.media existed
        entries = (run.review.body.get("media") if run.review else None) or []
    ordered = sorted(entries, key=lambda m: _ROLE_ORDER.get(m.get("role"), len(_ROLE_ORDER)))
    return [m["filename"] for m in ordered if m.get("filename")]


async def enqueue_review_workflow(review_input: ReviewInput, tenant: str) -> str | None:
    """Enqueues the review workflow on Hatchet. Separated for test mocking."""
    try:
        from vreview.workflows.review import review_workflow

        run_ref = await review_workflow.aio_run(
            review_input,
            wait_for_result=False,
            additional_metadata={"tenant": tenant, "run_id": review_input.run_id},
        )
        return getattr(run_ref, "workflow_run_id", None) or getattr(run_ref, "run_id", None)
    except Exception as exc:
        logger.warning("Could not enqueue review workflow on Hatchet: %s", exc)
        return None


def create_upload(
    tenant: str,
    filename: str,
    size: int,
    content_type: str | None = None,
    role: MediaRole = "candidate",
    project_id: str | None = None,
) -> dict[str, Any]:
    """Initiates multipart upload, creates asset record, and returns part presigned URLs."""
    if not filename or filename in (".", "..") or any(c in filename for c in "/\\\x00"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="filename must be a plain file name")
    try:
        validate_upload(filename, content_type, role)
    except UnsupportedMedia as exc:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)) from exc

    asset_id = gen_id("ast")
    upload_key = f"uploads/{asset_id}/{filename}"
    store = get_store()

    try:
        upload_id = store.create_multipart(upload_key, content_type=content_type)
    except Exception as exc:
        logger.error("Failed to create multipart upload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initiate upload: {exc}",
        ) from exc

    num_parts = max(1, math.ceil(size / PART_SIZE))
    parts = []
    for part_num in range(1, num_parts + 1):
        url = store.presign_part(upload_key, upload_id, part_num)
        parts.append({"part_number": part_num, "url": url})

    with get_sessionmaker()() as db:
        create_media_asset(
            db=db,
            tenant=tenant,
            filename=filename,
            size=size,
            upload_key=upload_key,
            upload_id=upload_id,
            role=role,
            project_id=project_id,
            asset_id=asset_id,
        )

    return {
        "asset_id": asset_id,
        "upload_id": upload_id,
        "upload_key": upload_key,
        "part_size": PART_SIZE,
        "parts": parts,
    }


def complete_upload(
    tenant: str,
    asset_id: str,
    parts: list[dict[str, Any]],
    upload_id: str | None = None,
) -> dict[str, Any]:
    """Completes multipart upload after parts have been uploaded."""
    with get_sessionmaker()() as db:
        asset = get_media_asset(db, tenant, asset_id, for_update=True)
        if not asset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
        if asset.status == ASSET_ABORTED:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload was aborted")

        eff_upload_id = upload_id or asset.upload_id
        if not eff_upload_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing upload_id for completing multipart upload",
            )

        store = get_store()
        try:
            store.complete_multipart(asset.upload_key, eff_upload_id, parts)
        except Exception as exc:
            logger.error("Failed to complete multipart upload for %s: %s", asset_id, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to complete upload: {exc}",
            ) from exc
        set_media_asset_status(db, asset, ASSET_COMPLETED)

    return {"asset_id": asset_id, "status": "completed"}


def abort_upload(tenant: str, asset_id: str) -> dict[str, Any]:
    """Aborts the multipart upload in the store and marks the asset aborted.

    Aborting twice is a no-op. A completed upload cannot be aborted (409).
    """
    with get_sessionmaker()() as db:
        asset = get_media_asset(db, tenant, asset_id, for_update=True)
        if not asset:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")
        if asset.status == ASSET_COMPLETED:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Upload already completed")

        if asset.status != ASSET_ABORTED:
            if asset.upload_id:
                try:
                    get_store().abort_multipart(asset.upload_key, asset.upload_id)
                except Exception as exc:
                    logger.error("Failed to abort multipart upload for %s: %s", asset_id, exc)
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to abort upload: {exc}",
                    ) from exc
            set_media_asset_status(db, asset, ASSET_ABORTED)

    return {"asset_id": asset_id, "status": "aborted"}


async def create_review(
    tenant: str,
    assets: list[dict[str, Any]],
    brief: dict[str, Any] | Brief,
    evaluators: list[str] | None = None,
    webhook_url: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Validates assets, creates queued run, and enqueues Hatchet review workflow."""
    if webhook_url:
        if not webhook_url.startswith("https://"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="webhook_url must use https protocol",
            )

    brief_obj = brief if isinstance(brief, Brief) else Brief(**brief)
    run_id = gen_id("run")

    media_inputs: list[MediaInput] = []
    run_media: list[dict[str, Any]] = []
    with get_sessionmaker()() as db:
        for item in assets:
            a_id = item.get("asset_id") if isinstance(item, dict) else getattr(item, "asset_id", None)
            role = item.get("role", "candidate") if isinstance(item, dict) else getattr(item, "role", "candidate")
            if not a_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Each asset must have an asset_id",
                )
            db_asset = get_media_asset(db, tenant, a_id)
            if not db_asset:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Asset {a_id} not found",
                )
            if db_asset.status == ASSET_ABORTED:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Asset {a_id} upload was aborted",
                )
            media_inputs.append(
                MediaInput(
                    asset_id=db_asset.id,
                    role=role,
                    key=db_asset.upload_key,
                    filename=db_asset.filename,
                    sha256=db_asset.sha256,
                )
            )
            run_media.append({"asset_id": db_asset.id, "role": role, "filename": db_asset.filename})

        create_run(
            db=db,
            tenant=tenant,
            run_id=run_id,
            brief=brief_obj.model_dump(mode="json"),
            evaluators=evaluators,
            webhook_url=webhook_url,
            project_id=project_id,
            status="queued",
            media=run_media,
        )

    review_input = ReviewInput(
        run_id=run_id,
        brief=brief_obj,
        media=media_inputs,
        evaluators=evaluators,
    )

    hatchet_run_id = await enqueue_review_workflow(review_input, tenant)

    if hatchet_run_id:
        with get_sessionmaker()() as db:
            update_run_hatchet_id(db, run_id, hatchet_run_id)

    return {
        "run_id": run_id,
        "status": "queued",
        "hatchet_run_id": hatchet_run_id,
    }


def get_review(tenant: str, run_id: str) -> dict[str, Any]:
    """Gets run status and review body if succeeded."""
    with get_sessionmaker()() as db:
        run = get_run(db, tenant, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review run not found")

        review_data: dict[str, Any] | None = None
        score: float | None = None

        review = repo_get_review(db, tenant, run_id)
        if review:
            review_data = _with_triage(review.body)
            score = review.score

        return {
            "run_id": run.id,
            "status": run.status,
            "stage": run.stage,
            "error": run.error,
            "score": score,
            "brief": run.brief,
            "evaluators": run.evaluators,
            "review": review_data,
            "hatchet_run_id": run.hatchet_run_id,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        }


def list_reviews(tenant: str, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """Returns paginated list of reviews for tenant."""
    with get_sessionmaker()() as db:
        runs, total = repo_list_runs(db, tenant, limit=limit, offset=offset)
        items = []
        for run in runs:
            score = run.review.score if run.review else None
            tri = _with_triage(run.review.body).get("triage") if run.review else None
            items.append({
                "run_id": run.id,
                "status": run.status,
                "stage": run.stage,
                "score": score,
                "verdict": tri["verdict"] if tri else None,
                "counts": tri["counts"] if tri else None,
                "filenames": _filenames(run),
                "error": run.error,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "updated_at": run.updated_at.isoformat() if run.updated_at else None,
            })

        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
        }


def get_issues(
    tenant: str,
    run_id: str,
    severity: str | None = None,
    category: str | None = None,
    evaluator: str | None = None,
    priority: str | None = None,
    area: str | None = None,
    thread_id: str | None = None,
) -> list[dict[str, Any]]:
    """Gets issues for a run, filtered by severity, category, evaluator, priority, area and thread."""
    with get_sessionmaker()() as db:
        run = get_run(db, tenant, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review run not found")

        issues = repo_get_issues(
            db,
            tenant,
            run_id,
            severity=severity,
            category=category,
            evaluator=evaluator,
        )
        legacy: dict[str, dict[str, Any]] | None = None
        out: list[dict[str, Any]] = []
        for issue in issues:
            body = issue.body
            if body.get("priority") is None:  # stored before triage existed
                if legacy is None:
                    legacy = _legacy_issue_fields(db, tenant, run_id)
                body = {**body, **legacy.get(issue.id, {})}
            if priority and body.get("priority") != priority:
                continue
            if area and body.get("area") != area:
                continue
            if thread_id and body.get("thread_id") != thread_id:
                continue
            out.append({
                "id": issue.id,
                "run_id": issue.run_id,
                "evaluator": issue.evaluator,
                "category": issue.category,
                "severity": issue.severity,
                "confidence": issue.confidence,
                "t0": issue.t0,
                "t1": issue.t1,
                "track": issue.track,
                "description": issue.description,
                "fix_hint": issue.fix_hint,
                "body": body,
            })
        return out


def get_artifact_url(tenant: str, run_id: str, name: str) -> str:
    """Returns presigned GET URL for an artifact."""
    if not name or ".." in name or name.startswith("/") or "\\" in name or "\x00" in name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid artifact name")

    with get_sessionmaker()() as db:
        run = get_run(db, tenant, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review run not found")

        key = review_key(run_id, name)
        store = get_store()
        return store.presign_get(key)


def get_media_url(tenant: str, run_id: str, asset_id: str) -> dict[str, Any]:
    """Presigned GET for one media file attached to a run (for the player)."""
    with get_sessionmaker()() as db:
        run = get_run(db, tenant, run_id)
        if not run:
            raise _not_found("Review run not found")

        review_row = repo_get_review(db, tenant, run_id)
        ingested = {m["asset_id"]: m for m in (review_row.body.get("media") or [])} if review_row else {}
        attached = {m["asset_id"]: m for m in (run.media or [])}
        entry = attached.get(asset_id) or ingested.get(asset_id)
        if entry is None:
            raise _not_found("Media not found for this review")

        asset = get_media_asset(db, tenant, asset_id)
        key = ingested.get(asset_id, {}).get("key") or (asset.upload_key if asset else None)
        if not key:
            raise _not_found("Media not found for this review")
        filename = entry.get("filename") or (asset.filename if asset else "") or ""
        role = entry.get("role") or (asset.role if asset else "candidate")

    return {
        "url": get_store().presign_get(key),
        "role": role,
        "filename": filename,
        "content_type": content_type_for(filename),
        "kind": kind_for(filename),
    }


def create_feedback(
    tenant: str,
    run_id: str,
    issue_id: str,
    verdict: str,
    comment: str | None = None,
) -> dict[str, Any]:
    """Records feedback for an issue."""
    try:
        verdict_enum = VerdictEnum(verdict)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid verdict '{verdict}'. Allowed: accept, false_positive, comment",
        )

    with get_sessionmaker()() as db:
        run = get_run(db, tenant, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review run not found")

        issue = db.get(IssueRow, (issue_id, run_id))
        if not issue:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")

        fb = repo_create_issue_feedback(
            db=db,
            tenant=tenant,
            run_id=run_id,
            issue_id=issue_id,
            verdict=verdict_enum,
            comment=comment,
        )
        return {"status": "ok", "feedback_id": fb.id}


def list_feedback(tenant: str, run_id: str, latest: bool = False) -> list[dict[str, Any]]:
    """Feedback for a run, oldest first. With latest=True, only the newest row per issue."""
    with get_sessionmaker()() as db:
        run = get_run(db, tenant, run_id)
        if not run:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Review run not found")

        rows = repo_list_issue_feedback(db, tenant, run_id)
        if latest:
            newest = {row.issue_id: row for row in rows}  # rows are oldest first, so later rows win
            rows = [row for row in rows if newest[row.issue_id] is row]
        return [
            {
                "issue_id": row.issue_id,
                "verdict": row.verdict.value,
                "comment": row.comment,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
