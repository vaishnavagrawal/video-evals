"""FastAPI router for vreview /v1 REST API endpoints."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from vreview.api.auth import get_current_tenant
from vreview.api import services
from vreview.schema import Brief, MediaRole

router = APIRouter(prefix="/v1")


class CreateUploadRequest(BaseModel):
    filename: str
    size: int
    content_type: str | None = None
    role: MediaRole = "candidate"
    project_id: str | None = None


class PartUploadInfo(BaseModel):
    part_number: int
    url: str


class CreateUploadResponse(BaseModel):
    asset_id: str
    upload_id: str
    upload_key: str
    part_size: int
    parts: list[PartUploadInfo]


class PartETag(BaseModel):
    PartNumber: int
    ETag: str


class CompleteUploadRequest(BaseModel):
    upload_id: str | None = None
    parts: list[PartETag]


class CompleteUploadResponse(BaseModel):
    asset_id: str
    status: str = "completed"


class AssetRoleInput(BaseModel):
    asset_id: str
    role: MediaRole = "candidate"


class CreateReviewRequest(BaseModel):
    assets: list[AssetRoleInput] | None = None
    asset_ids: list[str] | None = None
    roles: list[str] | None = None
    brief: Brief | dict[str, Any]
    evaluators: list[str] | None = None
    webhook_url: str | None = None
    project_id: str | None = None


class CreateReviewResponse(BaseModel):
    run_id: str
    status: str
    hatchet_run_id: str | None = None


class FeedbackRequest(BaseModel):
    verdict: Literal["accept", "false_positive", "comment"]
    comment: str | None = None


@router.post("/uploads", response_model=CreateUploadResponse, status_code=status.HTTP_201_CREATED)
def create_upload_endpoint(
    req: CreateUploadRequest,
    tenant: str = Depends(get_current_tenant),
):
    return services.create_upload(
        tenant=tenant,
        filename=req.filename,
        size=req.size,
        content_type=req.content_type,
        role=req.role,
        project_id=req.project_id,
    )


@router.post("/uploads/{asset_id}/complete", response_model=CompleteUploadResponse)
def complete_upload_endpoint(
    asset_id: str,
    req: CompleteUploadRequest,
    tenant: str = Depends(get_current_tenant),
):
    parts = [p.model_dump() for p in req.parts]
    return services.complete_upload(
        tenant=tenant,
        asset_id=asset_id,
        parts=parts,
        upload_id=req.upload_id,
    )


@router.post("/reviews", response_model=CreateReviewResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_review_endpoint(
    req: CreateReviewRequest,
    tenant: str = Depends(get_current_tenant),
):
    assets_list: list[dict[str, Any]] = []
    if req.assets:
        for a in req.assets:
            assets_list.append({"asset_id": a.asset_id, "role": a.role})
    elif req.asset_ids:
        roles = req.roles or ["candidate"] * len(req.asset_ids)
        for idx, a_id in enumerate(req.asset_ids):
            r = roles[idx] if idx < len(roles) else "candidate"
            assets_list.append({"asset_id": a_id, "role": r})

    return await services.create_review(
        tenant=tenant,
        assets=assets_list,
        brief=req.brief,
        evaluators=req.evaluators,
        webhook_url=req.webhook_url,
        project_id=req.project_id,
    )


@router.get("/reviews/{run_id}")
def get_review_endpoint(
    run_id: str,
    tenant: str = Depends(get_current_tenant),
):
    return services.get_review(tenant=tenant, run_id=run_id)


@router.get("/reviews")
def list_reviews_endpoint(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    tenant: str = Depends(get_current_tenant),
):
    return services.list_reviews(tenant=tenant, limit=limit, offset=offset)


@router.get("/reviews/{run_id}/issues")
def get_issues_endpoint(
    run_id: str,
    severity: str | None = Query(default=None),
    category: str | None = Query(default=None),
    evaluator: str | None = Query(default=None),
    tenant: str = Depends(get_current_tenant),
):
    return services.get_issues(
        tenant=tenant,
        run_id=run_id,
        severity=severity,
        category=category,
        evaluator=evaluator,
    )


@router.get("/reviews/{run_id}/artifacts/{name:path}")
def get_artifact_endpoint(
    run_id: str,
    name: str,
    redirect: bool = Query(default=False),
    accept: str | None = Header(default=None),
    tenant: str = Depends(get_current_tenant),
):
    url = services.get_artifact_url(tenant=tenant, run_id=run_id, name=name)
    if redirect or (accept and "text/html" in accept and "application/json" not in accept):
        return RedirectResponse(url=url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    return {"url": url}


@router.post("/reviews/{run_id}/issues/{issue_id}/feedback")
def create_feedback_endpoint(
    run_id: str,
    issue_id: str,
    req: FeedbackRequest,
    tenant: str = Depends(get_current_tenant),
):
    return services.create_feedback(
        tenant=tenant,
        run_id=run_id,
        issue_id=issue_id,
        verdict=req.verdict,
        comment=req.comment,
    )
