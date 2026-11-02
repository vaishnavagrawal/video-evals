"""FastAPI application entry point for vreview."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from vreview.api.mcp import mcp_server
from vreview.api.routes import router

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
