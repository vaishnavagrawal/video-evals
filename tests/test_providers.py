"""dubeval.providers: model references resolve to the right provider without code changes."""
from __future__ import annotations

import pytest

from dubeval import providers


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith(("LLM_PROVIDER_", "LLM_DEFAULT_PROVIDER", "DUBEVAL_LLM_", "DUBEVAL_ASR_", "OMNIROUTE_",
                         "OPENAI_", "META_API_KEY", "MODEL_API_KEY", "FIREWORKS_")):
            monkeypatch.delenv(k, raising=False)
    providers._clients.clear()


def test_prefix_selects_known_provider_only():
    assert str(providers.parse("meta:muse-spark-1.3")) == "meta:muse-spark-1.3"
    assert str(providers.parse("gemini-flash")) == "gateway:gemini-flash"
    # colons inside model ids are not provider prefixes
    assert str(providers.parse("auto/coding:fast")) == "gateway:auto/coding:fast"
    assert str(providers.parse("bedrock/meta.llama3-1-70b-instruct-v1:0")).startswith("gateway:bedrock/")
    assert str(providers.parse("nosuch:model")) == "gateway:nosuch:model"


def test_default_provider_from_env(monkeypatch):
    monkeypatch.setenv("LLM_DEFAULT_PROVIDER", "meta")
    assert str(providers.parse("muse-spark-1.3")) == "meta:muse-spark-1.3"


def test_meta_preset_reads_meta_key(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "k-meta")
    p = providers.provider("meta")
    assert (p.base_url, p.api_key, p.video) == ("https://api.meta.ai/v1", "k-meta", "video_url")


def test_env_overrides_preset_and_adds_new_providers(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_META_BASE_URL", "https://proxy.example/v1")
    monkeypatch.setenv("LLM_PROVIDER_META_API_KEY", "k2")
    monkeypatch.setenv("LLM_PROVIDER_META_VIDEO", "frames")
    p = providers.provider("meta")
    assert (p.base_url, p.api_key, p.video) == ("https://proxy.example/v1", "k2", "frames")

    monkeypatch.setenv("LLM_PROVIDER_LOCALVLLM_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.setenv("VLLM_TOKEN", "t")
    monkeypatch.setenv("LLM_PROVIDER_LOCALVLLM_API_KEY_ENV", "VLLM_TOKEN")
    assert str(providers.parse("localvllm:llama-4-scout")) == "localvllm:llama-4-scout"
    assert providers.provider("localvllm").api_key == "t"


def test_gateway_keeps_legacy_variables(monkeypatch):
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "https://gw/v1")
    monkeypatch.setenv("OMNIROUTE_API_KEY", "g")
    monkeypatch.setenv("DUBEVAL_ASR_API_KEY", "asr")
    assert providers.provider("gateway").api_key == "g"
    assert providers.provider("gateway", legacy_key_envs=("DUBEVAL_ASR_API_KEY",)).api_key == "asr"
    assert providers.provider("gateway").base_url == "https://gw/v1"


def test_missing_key_and_unknown_provider_raise():
    with pytest.raises(providers.ProviderError, match="META_API_KEY"):
        providers.provider("meta")
    with pytest.raises(providers.ProviderError, match="unknown"):
        providers.provider("nosuch")


def test_routed_client_strips_prefix_and_routes(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "km")
    monkeypatch.setenv("OMNIROUTE_API_KEY", "kg")
    seen = []

    class Fake:
        def __init__(self, name):
            self.chat = type("C", (), {"completions": type("X", (), {
                "create": staticmethod(lambda **kw: seen.append((name, kw["model"])))})()})()

    monkeypatch.setattr(providers, "client_for", lambda p, **kw: Fake(p.name))
    c = providers.RoutedClient()
    c.chat.completions.create(model="meta:muse-spark-1.3", messages=[])
    c.chat.completions.create(model="gemini-flash", messages=[])
    assert seen == [("meta", "muse-spark-1.3"), ("gateway", "gemini-flash")]
