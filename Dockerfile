# One image for both the api (`vreview-api`, the default) and the worker (`vreview-worker`).
# The build needs only pyproject.toml, uv.lock and src/ (pyproject has no `readme`); alembic.ini, alembic/ and
# docker/entrypoint.sh are copied into the runtime stage. Everything else is excluded by .dockerignore.

# ---- builder: resolve and install the locked dependencies, then the project itself (non-editable) ----
FROM python:3.12-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_PROGRESS=1

COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first: this layer is reused until pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra platform

COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable --extra platform

# ---- runtime ----
FROM python:3.12-slim-bookworm AS runtime

# libgles2 and libegl1: MediaPipe's face landmarker dlopen()s libGLESv2 even on CPU.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
        libgles2 \
        libegl1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Fixed uid so a volume mounted at /data keeps its ownership across rebuilds.
RUN groupadd --gid 10001 vreview \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /data/home --shell /usr/sbin/nologin vreview \
    && mkdir -p /data/home /data/cache /data/scratch \
    && chown -R vreview:vreview /data

# Everything the process writes lives under /data, so one volume can hold it:
#   HOME           -> ~/.cache/dubeval/face_landmarker.task (MediaPipe model, path hard-coded in dubeval.visual)
#   HF_HOME        -> faster-whisper models
#   TORCH_HOME     -> demucs weights
#   VREVIEW_SCRATCH_DIR, TMPDIR -> per-run downloads and temporary files
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    HOME=/data/home \
    XDG_CACHE_HOME=/data/cache \
    HF_HOME=/data/cache/huggingface \
    TORCH_HOME=/data/cache/torch \
    VREVIEW_SCRATCH_DIR=/data/scratch \
    TMPDIR=/data/scratch/tmp

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY alembic ./alembic
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/vreview-entrypoint

USER vreview
EXPOSE 8000

# Meaningful for the api only. The worker service overrides it in compose.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

ENTRYPOINT ["vreview-entrypoint"]
CMD ["vreview-api"]
