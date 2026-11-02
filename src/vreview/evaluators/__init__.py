from __future__ import annotations

from vreview.evaluator import Evaluator

from .dubbing import DubbingEvaluator

REGISTRY: dict[str, Evaluator] = {
    "dubbing": DubbingEvaluator(),
}

__all__ = ["REGISTRY", "DubbingEvaluator"]
