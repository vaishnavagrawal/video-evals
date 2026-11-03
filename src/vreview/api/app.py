"""FastAPI application entry point for vreview."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from vreview.api.mcp import mcp_server
from vreview.api.routes import router
from vreview.config import settings

mcp_app = mcp_server.http_app(path="/", stateless_http=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize FastMCP lifespan
    async with mcp_app.router.lifespan_context(mcp_app):
        yield


app = FastAPI(
    title="vreview",
    description="Video evaluation & review platform API",
    version="1.0.0",
    lifespan=lifespan,
)

# The web app calls the API from the browser with a Bearer token (no cookies, so no credentials mode).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().cors_origins,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# Unauthenticated healthcheck
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Mount /v1 REST API
app.include_router(router)

# Mount /mcp FastMCP server
app.mount("/mcp", mcp_app)


def main() -> None:
    import uvicorn

    uvicorn.run("vreview.api.app:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()
