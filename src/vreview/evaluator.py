"""Evaluator plugin contract.

An evaluator is a named, versioned unit that turns ingested media + brief into an EvaluatorResult.
Heavy sub-stages (stems, ASR, ...) are exposed separately by each evaluator package so the workflow
can run them as their own Hatchet tasks; `run` is the single-process path (tests, CLI, small jobs).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .schema import Brief, EvaluatorResult, MediaRef
from .storage.base import BlobStore


@dataclass(frozen=True)
class EvalContext:
    run_id: str
    brief: Brief
    media: list[MediaRef]
    store: BlobStore


@runtime_checkable
class Evaluator(Protocol):
    id: str
    version: str

    def applies_to(self, brief: Brief, media: list[MediaRef]) -> str | None:
        """None if applicable, else the reason it is skipped."""

    def run(self, ctx: EvalContext) -> EvaluatorResult: ...
