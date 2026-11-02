"""Hatchet client accessor.

Hatchet requires HATCHET_CLIENT_TOKEN at client construction time.
To allow module imports (such as in tests, API endpoints, or CLI tools)
without requiring credentials upfront, get_hatchet() lazily constructs
and caches the Hatchet client.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from hatchet_sdk import Hatchet
from hatchet_sdk.runnables.task import normalize_validator
from hatchet_sdk.runnables.workflow import TaskDefaults, Workflow, WorkflowConfig
from hatchet_sdk.types.priority import Priority
from pydantic import TypeAdapter


@lru_cache(maxsize=1)
def get_hatchet() -> Hatchet:
    """Return a cached Hatchet client instance.

    Reads HATCHET_CLIENT_* environment variables.
    """
    return Hatchet()


class _LazyConfig:
    """Fallback config when Hatchet client has not yet been initialized."""

    namespace: str = ""

    def apply_namespace(self, resource_name: str | None, namespace_override: str | None = None) -> str | None:
        if resource_name is None:
            return None
        namespace = namespace_override or self.namespace
        if not namespace:
            return resource_name
        if resource_name.startswith(namespace):
            return resource_name
        return namespace + resource_name


class _LazyHatchetProxy:
    """Proxy that defers Hatchet initialization until attributes are accessed.

    This allows defining workflows at module scope without triggering client instantiation
    and token validation at import time.
    """

    @property
    def config(self) -> Any:
        try:
            return get_hatchet().config
        except Exception:
            return _LazyConfig()

    @property
    def namespace(self) -> str:
        try:
            return get_hatchet().namespace
        except Exception:
            return ""

    def __getattr__(self, name: str) -> Any:
        return getattr(get_hatchet(), name)

    def workflow(
        self,
        *,
        name: str,
        description: str | None = None,
        input_validator: Any = None,
        on_events: list[str] | None = None,
        on_crons: list[str] | None = None,
        cron_input: Any = None,
        version: str | None = None,
        sticky: Any = None,
        default_priority: Any = Priority.LOW,
        concurrency: Any = None,
        task_defaults: Any = None,
        default_filters: list[Any] | None = None,
        default_additional_metadata: Any = None,
        idempotency: Any = None,
    ) -> Workflow:
        return Workflow(
            WorkflowConfig(
                name=name,
                description=description,
                on_events=on_events or [],
                on_crons=on_crons or [],
                cron_input=cron_input,
                version=version,
                sticky=sticky,
                concurrency=concurrency,
                input_validator=TypeAdapter(normalize_validator(input_validator)),
                task_defaults=task_defaults or TaskDefaults(),
                default_priority=default_priority,
                default_filters=default_filters or [],
                default_additional_metadata=default_additional_metadata or {},
                idempotency=idempotency,
            ),
            self,
        )


hatchet = _LazyHatchetProxy()
