"""Rerank settings validation (#1161 review).

``rerank_base_url`` is used as a truthy feature toggle (``if
settings.rerank_base_url:``) and ``rerank_model`` is sent verbatim to the
endpoint, so a stray-whitespace env value must not enable the vLLM path with an
invalid URL nor ship a blank model name. These pin the strip/normalize contract.
"""

from __future__ import annotations

import pytest

from config.settings import Settings

_RERANK_ENV_KEYS = ["RERANK_BASE_URL", "RERANK_MODEL"]


@pytest.fixture
def clean_rerank_env(monkeypatch):
    for k in _RERANK_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _fresh_settings() -> Settings:
    # _env_file=None ignores .env.dev so only os.environ (monkeypatched) is read.
    return Settings(_env_file=None)


def test_defaults_when_unset(clean_rerank_env):
    s = _fresh_settings()
    assert s.rerank_base_url == ""  # empty = local vLLM path disabled
    assert s.rerank_model == "qwen3-reranker-0.6b"


def test_legacy_embedding_provider_ollama_coerced_to_self_hosted(monkeypatch):
    """v0.42 review #4: the retired EMBEDDING_PROVIDER=ollama value must coerce
    to 'self_hosted' (with a warning), not be accepted verbatim and route
    self-hosted embedding traffic into the OpenAI client path."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "ollama")
    with pytest.warns(UserWarning, match="EMBEDDING_PROVIDER=ollama is retired"):
        s = Settings(_env_file=None)
    assert s.embedding_provider == "self_hosted"


def test_embedding_provider_self_hosted_unchanged(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "self_hosted")
    s = Settings(_env_file=None)
    assert s.embedding_provider == "self_hosted"


def test_whitespace_only_base_url_collapses_to_empty(clean_rerank_env):
    """A whitespace-only RERANK_BASE_URL must NOT enable the vLLM path."""
    clean_rerank_env.setenv("RERANK_BASE_URL", "   ")
    s = _fresh_settings()
    assert s.rerank_base_url == ""
    assert not s.rerank_base_url  # truthy-toggle stays off


def test_surrounding_whitespace_stripped(clean_rerank_env):
    clean_rerank_env.setenv("RERANK_BASE_URL", "  http://gpu:8002\n")
    clean_rerank_env.setenv("RERANK_MODEL", "  qwen3-reranker-4b  ")
    s = _fresh_settings()
    assert s.rerank_base_url == "http://gpu:8002"
    assert s.rerank_model == "qwen3-reranker-4b"


def test_whitespace_only_model_collapses_to_empty(clean_rerank_env):
    """A blank RERANK_MODEL collapses to '' so the resolver falls back to the
    built-in default instead of shipping a whitespace model name."""
    clean_rerank_env.setenv("RERANK_MODEL", "  ")
    s = _fresh_settings()
    assert s.rerank_model == ""
