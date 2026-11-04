"""FastMCP server exposing review tools with JWT bearer auth."""
from __future__ import annotations

import logging
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_http_request

from vreview.api.auth import verify_jwt
from vreview.api import services

logger = logging.getLogger(__name__)


class VReviewTokenVerifier(TokenVerifier):
    """Verifies bearer JWT tokens against JWKS for MCP connections."""

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            tenant, claims = verify_jwt(token)
            return AccessToken(
                token=token,
                client_id=tenant,
                subject=tenant,
                scopes=[],
                claims=claims,
            )
        except Exception as exc:
            logger.debug("MCP token verification rejected: %s", exc)
            return None


mcp_server = FastMCP("vreview", auth=VReviewTokenVerifier())


def _extract_tenant(ctx: Context | None = None) -> str:
    try:
        req = get_http_request()
        user = req.scope.get("user")
        if user and getattr(user, "username", None):
            return user.username
        if user and getattr(user, "access_token", None):
            return user.access_token.client_id
    except Exception:
        pass

    if ctx and ctx.client_id:
        return ctx.client_id

    raise RuntimeError("Could not determine authenticated tenant for MCP tool call")


@mcp_server.tool
async def review_video(
    asset_ids: list[str],
    roles: list[str] | None = None,
    brief: dict[str, Any] | None = None,
    webhook_url: str | None = None,
    evaluators: list[str] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Enqueue a review run for uploaded video assets."""
    tenant = _extract_tenant(ctx)
    eff_roles = roles or ["candidate"] * len(asset_ids)
    assets = [
        {"asset_id": a_id, "role": r}
        for a_id, r in zip(asset_ids, eff_roles)
    ]
    return await services.create_review(
        tenant=tenant,
        assets=assets,
        brief=brief or {"kind": "dub"},
        evaluators=evaluators,
        webhook_url=webhook_url,
    )


@mcp_server.tool
def get_review(
    run_id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Get the status and review results of a video review run."""
    tenant = _extract_tenant(ctx)
    return services.get_review(tenant=tenant, run_id=run_id)


@mcp_server.tool
def get_pipeline(
    run_id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Get how a review run is progressing: every pipeline stage and child step with its status, timing and detail."""
    tenant = _extract_tenant(ctx)
    return services.get_pipeline(tenant=tenant, run_id=run_id)


@mcp_server.tool
def get_issues(
    run_id: str,
    severity: str | None = None,
    category: str | None = None,
    evaluator: str | None = None,
    ctx: Context | None = None,
) -> list[dict[str, Any]]:
    """Get issues found in a review run, filtered by severity, category, or evaluator."""
    tenant = _extract_tenant(ctx)
    return services.get_issues(
        tenant=tenant,
        run_id=run_id,
        severity=severity,
        category=category,
        evaluator=evaluator,
    )
