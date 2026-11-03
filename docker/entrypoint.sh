#!/bin/sh
# Entry point for the api and worker containers.
#   VREVIEW_RUN_MIGRATIONS=1  run `alembic upgrade head` before starting (set it on the api service only).
set -eu

# Cache and scratch directories (see the Dockerfile). A fresh named volume inherits the ownership of /data in the
# image; a bind mount may not, so fail early with a readable message instead of deep inside a review run.
for dir in "$HOME" "$XDG_CACHE_HOME" "$HF_HOME" "$TORCH_HOME" "$VREVIEW_SCRATCH_DIR" "$TMPDIR"; do
    if ! mkdir -p "$dir" 2>/dev/null || [ ! -w "$dir" ]; then
        echo "entrypoint: $dir is not writable by uid $(id -u). Check the ownership of the volume mounted at /data" \
             "(chown -R $(id -u):$(id -g))." >&2
        exit 1
    fi
done

# Scratch is never authoritative. Drop leftovers of runs that were killed mid-flight (a run's own directory is
# removed by the workflow when it ends).
find "$VREVIEW_SCRATCH_DIR" -mindepth 1 -maxdepth 1 -not -path "$TMPDIR" -mmin +1440 -exec rm -rf {} + 2>/dev/null || true
find "$TMPDIR" -mindepth 1 -maxdepth 1 -mmin +1440 -exec rm -rf {} + 2>/dev/null || true

if [ "${VREVIEW_RUN_MIGRATIONS:-0}" = "1" ]; then
    attempt=1
    max_attempts=10
    until alembic upgrade head; do
        if [ "$attempt" -ge "$max_attempts" ]; then
            echo "entrypoint: alembic upgrade head failed after $max_attempts attempts" >&2
            exit 1
        fi
        echo "entrypoint: alembic upgrade head failed (attempt $attempt/$max_attempts), retrying in 3s" >&2
        attempt=$((attempt + 1))
        sleep 3
    done
fi

exec "$@"
