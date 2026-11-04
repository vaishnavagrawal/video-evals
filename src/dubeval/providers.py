"""Model providers: one place that turns a model reference into an OpenAI-compatible client.

Every model call in dubeval and vreview goes through `resolve(ref)`. Nothing else knows a base URL,
an API key variable or a vendor name, so swapping a model or a vendor is a config change.

Model reference: ``"<provider>:<model>"`` or a bare ``"<model>"``.
- ``meta:muse-spark-1.3``             -> provider ``meta``, model ``muse-spark-1.3``
- ``gateway:gemini-flash``            -> provider ``gateway``
- ``gemini-flash``                    -> the default provider (``LLM_DEFAULT_PROVIDER``, else ``gateway``)
The prefix only counts as a provider when it names a known provider, so model ids that contain a
colon (``bedrock/meta.llama3-1-70b-instruct-v1:0``, ``auto/coding:fast``) pass through unchanged.

A provider is configured from the environment, ``<NAME>`` being the upper-cased provider name:
- ``LLM_PROVIDER_<NAME>_BASE_URL``   OpenAI-compatible base URL
- ``LLM_PROVIDER_<NAME>_API_KEY``    the key itself, or
- ``LLM_PROVIDER_<NAME>_API_KEY_ENV`` the name of the variable that holds it
- ``LLM_PROVIDER_<NAME>_VIDEO``      how it takes video: ``video_url`` (data URL part), ``frames`` (images), ``none``
- ``LLM_PROVIDER_<NAME>_TIMEOUT``    request timeout in seconds
Any name works (``LLM_PROVIDER_FIREWORKS_BASE_URL=...`` makes ``fireworks:<model>`` valid). Presets below
fill defaults for common providers; env always wins over a preset.

The ``gateway`` preset keeps the variables this project used before the registry existed
(``DUBEVAL_LLM_*``, ``OMNIROUTE_*``, ``OPENAI_*``), so existing deployments need no change.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Literal

VideoMode = Literal["video_url", "frames", "none"]


@dataclass(frozen=True)
class _Preset:
    base_url: str | None = None
    base_url_envs: tuple[str, ...] = ()
    key_envs: tuple[str, ...] = ()
    video: VideoMode = "video_url"


# Defaults only. Add a provider without code by setting LLM_PROVIDER_<NAME>_* variables.
PRESETS: dict[str, _Preset] = {
    # Meta Model API: OpenAI-compatible; Chat Completions takes video through a `video_url` part.
    "meta": _Preset(base_url="https://api.meta.ai/v1", key_envs=("META_API_KEY", "MODEL_API_KEY")),
    "openai": _Preset(base_url="https://api.openai.com/v1", key_envs=("OPENAI_API_KEY",), video="frames"),
    "openrouter": _Preset(base_url="https://openrouter.ai/api/v1", key_envs=("OPENROUTER_API_KEY",)),
    "fireworks": _Preset(base_url="https://api.fireworks.ai/inference/v1", key_envs=("FIREWORKS_API_KEY",),
                         video="frames"),
    "groq": _Preset(base_url="https://api.groq.com/openai/v1", key_envs=("GROQ_API_KEY",), video="none"),
    # Any OpenAI-compatible router (OmniRoute, LiteLLM, ...). Legacy variable names.
    "gateway": _Preset(base_url_envs=("DUBEVAL_LLM_BASE_URL", "OMNIROUTE_BASE_URL", "OPENAI_BASE_URL"),
                       key_envs=("DUBEVAL_LLM_API_KEY", "OMNIROUTE_API_KEY", "OPENAI_API_KEY")),
}
DEFAULT_TIMEOUT_S = 150.0


class ProviderError(RuntimeError):
    """The provider of a model reference is unknown or has no credentials."""


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str | None
    api_key: str
    video: VideoMode
    timeout: float


@dataclass(frozen=True)
class ModelRef:
    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else None


def _first_env(names: tuple[str, ...]) -> str | None:
    for n in names:
        if v := _env(n):
            return v
    return None


def known_providers() -> set[str]:
    names = set(PRESETS)
    for key in os.environ:
        if key.startswith("LLM_PROVIDER_") and key.endswith("_BASE_URL"):
            names.add(key[len("LLM_PROVIDER_"):-len("_BASE_URL")].lower())
    return names


def default_provider() -> str:
    return (_env("LLM_DEFAULT_PROVIDER") or "gateway").lower()


def parse(ref: str, default: str | None = None) -> ModelRef:
    ref = ref.strip()
    head, sep, tail = ref.partition(":")
    if sep and tail and "/" not in head and head.lower() in known_providers():
        return ModelRef(head.lower(), tail)
    return ModelRef((default or default_provider()).lower(), ref)


def provider(name: str, *, legacy_key_envs: tuple[str, ...] = (),
             legacy_base_url_envs: tuple[str, ...] = ()) -> Provider:
    """Resolve provider settings. `legacy_*` are checked first for the gateway provider only (lets ASR keep
    its own DUBEVAL_ASR_* credentials)."""
    name = name.lower()
    up = name.upper().replace("-", "_")
    preset = PRESETS.get(name, _Preset())
    legacy = name == "gateway"
    base_url = (_env(f"LLM_PROVIDER_{up}_BASE_URL")
                or (_first_env(legacy_base_url_envs) if legacy else None)
                or _first_env(preset.base_url_envs) or preset.base_url)
    key_env = _env(f"LLM_PROVIDER_{up}_API_KEY_ENV")
    api_key = (_env(f"LLM_PROVIDER_{up}_API_KEY") or (_env(key_env) if key_env else None)
               or (_first_env(legacy_key_envs) if legacy else None) or _first_env(preset.key_envs))
    if name not in PRESETS and not _env(f"LLM_PROVIDER_{up}_BASE_URL"):
        raise ProviderError(f"unknown model provider {name!r}; set LLM_PROVIDER_{up}_BASE_URL")
    if not api_key:
        hint = key_env or (preset.key_envs[0] if preset.key_envs else f"LLM_PROVIDER_{up}_API_KEY")
        raise ProviderError(f"no API key for model provider {name!r}; set {hint}")
    video = (_env(f"LLM_PROVIDER_{up}_VIDEO") or preset.video).lower()
    if video not in ("video_url", "frames", "none"):
        raise ProviderError(f"LLM_PROVIDER_{up}_VIDEO must be video_url, frames or none, got {video!r}")
    timeout = float(_env(f"LLM_PROVIDER_{up}_TIMEOUT") or DEFAULT_TIMEOUT_S)
    return Provider(name, base_url, api_key, video, timeout)  # type: ignore[arg-type]


_clients: dict[tuple, Any] = {}
_lock = threading.Lock()


def client_for(p: Provider, *, timeout: float | None = None, max_retries: int = 0):
    from openai import OpenAI

    key = (p.name, p.base_url, p.api_key, timeout or p.timeout, max_retries)
    with _lock:
        if key not in _clients:
            _clients[key] = OpenAI(api_key=p.api_key, base_url=p.base_url, timeout=timeout or p.timeout,
                                   max_retries=max_retries)
        return _clients[key]


@dataclass(frozen=True)
class Resolved:
    ref: ModelRef
    provider: Provider
    client: Any

    @property
    def model(self) -> str:
        return self.ref.model

    @property
    def label(self) -> str:
        return str(self.ref)


def resolve(ref: str, *, timeout: float | None = None, max_retries: int = 0, default: str | None = None,
            legacy_key_envs: tuple[str, ...] = (), legacy_base_url_envs: tuple[str, ...] = ()) -> Resolved:
    """Model reference -> (provider settings, OpenAI client, provider-side model id). Raises ProviderError."""
    mref = parse(ref, default)
    p = provider(mref.provider, legacy_key_envs=legacy_key_envs, legacy_base_url_envs=legacy_base_url_envs)
    return Resolved(mref, p, client_for(p, timeout=timeout, max_retries=max_retries))


class _Endpoint:
    def __init__(self, router: "RoutedClient", path: tuple[str, ...]):
        self._router, self._path = router, path

    def create(self, *, model: str, **kw):
        r = self._router.resolve(model)
        target = r.client
        for part in self._path:
            target = getattr(target, part)
        return target.create(model=r.model, **kw)


class RoutedClient:
    """Drop-in for an OpenAI client whose `model=` argument is a model reference.

    `client.chat.completions.create(model="meta:muse-spark-1.3", ...)` resolves the provider per call, so
    one client serves a fallback list that spans vendors. A reference whose provider has no credentials
    raises ProviderError at call time; callers that walk a model list treat it like any other failure.
    """

    def __init__(self, *, timeout: float | None = None, max_retries: int = 0,
                 legacy_key_envs: tuple[str, ...] = (), legacy_base_url_envs: tuple[str, ...] = ()):
        self._kw = dict(timeout=timeout, max_retries=max_retries, legacy_key_envs=legacy_key_envs,
                        legacy_base_url_envs=legacy_base_url_envs)
        from types import SimpleNamespace

        self.chat = SimpleNamespace(completions=_Endpoint(self, ("chat", "completions")))
        self.audio = SimpleNamespace(transcriptions=_Endpoint(self, ("audio", "transcriptions")))

    def resolve(self, ref: str) -> Resolved:
        return resolve(ref, **self._kw)


def split_csv(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else value
    return list(dict.fromkeys(s.strip() for s in items if isinstance(s, str) and s.strip()))
