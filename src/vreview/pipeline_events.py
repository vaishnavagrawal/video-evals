"""Pipeline events: what a review run is doing right now, as data the UI can draw.

Any stage of any evaluator can emit nodes; nothing here knows about dubbing or Hatchet. A node is one row of
`pipeline_events` per (run_id, node), updated in place as it goes running -> succeeded | failed | skipped.
Children point at their stage through `parent` (e.g. node "dubbing_features/<slug>", parent "dubbing_features").
`detail` is a free-form JSON dict (model, issues, cached, score, error, ...) that the UI shows as chips.

    with pipeline_events.stage(run_id, "av_judge", "AV judge") as st:
        st.update(model="gemini-pro")       # visible to the UI immediately
        ...                                 # raises -> node failed (detail.error) and the exception propagates
        st.update(issues=3)                 # leaving the block normally -> node succeeded

Recording is best effort: a database problem is logged as a warning and never reaches the caller, so a DB
hiccup cannot fail a review.
"""
from __future__ import annotations

import asyncio
import logging
import math
import numbers
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from vreview.db.models import PipelineEventRow, RunRow, utcnow
from vreview.db.session import get_sessionmaker

logger = logging.getLogger(__name__)

RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"
TERMINAL = frozenset({SUCCEEDED, FAILED, SKIPPED})

KIND_STAGE = "stage"
KIND_CHILD = "child"

_MAX_ERROR_CHARS = 500


def _clean(value: Any) -> Any:
    """JSON-safe copy: str keys, finite numbers only, anything unknown as its str()."""
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_clean(v) for v in value]
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        number = float(value)
        return number if math.isfinite(number) else None
    return str(value)


def _error_text(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    text = f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return text[:_MAX_ERROR_CHARS]


def _upsert(
    session: Session,
    run_id: str,
    node: str,
    *,
    status: str,
    label: str | None,
    parent: str | None,
    kind: str | None,
    detail: dict[str, Any] | None,
) -> None:
    now = utcnow()
    row = session.scalars(
        select(PipelineEventRow).where(PipelineEventRow.run_id == run_id, PipelineEventRow.node == node)
    ).first()

    if row is None:
        session.add(
            PipelineEventRow(
                run_id=run_id,
                node=node,
                parent=parent,
                label=label or node,
                kind=kind or (KIND_CHILD if parent else KIND_STAGE),
                status=status,
                started_at=now,
                finished_at=now if status in TERMINAL else None,
                detail={k: v for k, v in (detail or {}).items() if v is not None},
            )
        )
        return

    merged = dict(row.detail or {})
    # A finished node going back to running is a retry: restart its clock and drop the previous error.
    restarted = status == RUNNING and row.status in TERMINAL
    if restarted:
        row.started_at = now
        row.finished_at = None
        merged.pop("error", None)
        merged["attempt"] = int(merged.get("attempt") or 1) + 1
    for key, value in (detail or {}).items():
        if value is None:
            merged.pop(key, None)  # passing None clears a key
        else:
            merged[key] = value
    row.detail = merged  # a new dict, so the JSON column registers the change

    if label:
        row.label = label
    if parent is not None:
        row.parent = parent
    if kind:
        row.kind = kind
    if status in TERMINAL and (row.finished_at is None or row.status != status):
        row.finished_at = now
    row.status = status


def record(
    run_id: str,
    node: str,
    *,
    status: str,
    label: str | None = None,
    parent: str | None = None,
    kind: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Create or update the node `node` of run `run_id`. Never raises.

    The first call sets started_at (and finished_at if the status is already terminal); later calls change
    the status, merge `detail` into the stored detail (a None value removes its key) and set finished_at when
    the status becomes terminal. label, parent and kind are only changed when given; a new node defaults to
    kind "child" if it has a parent, else "stage".
    """
    try:
        clean = _clean(detail) if detail else None
        for attempt in (1, 2):
            try:
                with get_sessionmaker()() as session:
                    _upsert(
                        session, run_id, node,
                        status=status, label=label, parent=parent, kind=kind, detail=clean,
                    )
                    session.commit()
                return
            except IntegrityError:
                # Lost a race with a concurrent insert of the same node: the second pass updates that row.
                # (Also raised for an unknown run_id; then the second pass fails too and is swallowed below.)
                if attempt == 2:
                    raise
    except Exception as exc:
        logger.warning("Pipeline event not recorded (run=%s node=%s status=%s): %s", run_id, node, status, exc)


def fail_running(run_id: str, error: str | None = None) -> None:
    """Mark every node of the run that is still running as failed (the workflow died). Never raises."""
    try:
        with get_sessionmaker()() as session:
            rows = session.scalars(
                select(PipelineEventRow).where(PipelineEventRow.run_id == run_id, PipelineEventRow.status == RUNNING)
            ).all()
            now = utcnow()
            for row in rows:
                row.status = FAILED
                row.finished_at = now
                if error and "error" not in (row.detail or {}):
                    row.detail = {**(row.detail or {}), "error": error[:_MAX_ERROR_CHARS]}
            session.commit()
    except Exception as exc:
        logger.warning("Could not mark running pipeline nodes of run %s as failed: %s", run_id, exc)


class StageHandle:
    """What `stage()` yields: add detail with `.update(...)`, or end the node as skipped with `.skip(...)`."""

    def __init__(self, run_id: str, node: str, *, live: bool = True) -> None:
        self.run_id = run_id
        self.node = node
        self.detail: dict[str, Any] = {}
        self._live = live  # write each update right away (sync stages); async stages flush on exit
        self._outcome = SUCCEEDED
        self._closed = False

    def update(self, **detail: Any) -> None:
        """Add keys to the node's detail (a None value removes the key)."""
        self.detail.update(detail)
        if self._live and not self._closed:
            record(self.run_id, self.node, status=RUNNING, detail=detail)

    def skip(self, reason: str | None = None) -> None:
        """End the node as `skipped` instead of `succeeded` when the block completes normally."""
        self._outcome = SKIPPED
        if reason:
            self.detail["reason"] = reason

    def _finish(self, exc: BaseException | None) -> None:
        self._closed = True
        if exc is None:
            record(self.run_id, self.node, status=self._outcome, detail=self.detail)
        else:
            record(self.run_id, self.node, status=FAILED, detail={**self.detail, "error": _error_text(exc)})


@contextmanager
def stage(
    run_id: str,
    node: str,
    label: str | None = None,
    parent: str | None = None,
    kind: str | None = None,
) -> Iterator[StageHandle]:
    """Record `node` as running, then succeeded when the block ends, or failed (detail.error) if it raises."""
    handle = StageHandle(run_id, node)
    record(run_id, node, status=RUNNING, label=label, parent=parent, kind=kind)
    try:
        yield handle
    except BaseException as exc:
        handle._finish(exc)
        raise
    handle._finish(None)


@asynccontextmanager
async def astage(
    run_id: str,
    node: str,
    label: str | None = None,
    parent: str | None = None,
    kind: str | None = None,
) -> AsyncIterator[StageHandle]:
    """`stage()` for async tasks: the database writes run in a thread, so the event loop is not blocked.
    `.update()` only collects detail here; it is written when the node ends."""
    handle = StageHandle(run_id, node, live=False)
    await asyncio.to_thread(record, run_id, node, status=RUNNING, label=label, parent=parent, kind=kind)
    try:
        yield handle
    except BaseException as exc:
        await asyncio.to_thread(handle._finish, exc)
        raise
    await asyncio.to_thread(handle._finish, None)


def list_events(db: Session, tenant: str, run_id: str) -> list[PipelineEventRow]:
    """The run's nodes, oldest start first. Empty for a run of another tenant (same as for no events)."""
    stmt = (
        select(PipelineEventRow)
        .join(RunRow, PipelineEventRow.run_id == RunRow.id)
        .where(PipelineEventRow.run_id == run_id, RunRow.tenant == tenant)
        .order_by(PipelineEventRow.started_at, PipelineEventRow.id)
    )
    return list(db.scalars(stmt).all())
