"""Rerank settings validation (#1161 review).

``rerank_base_url`` is used as a truthy feature toggle (``if
settings.rerank_base_url:``) and ``rerank_model`` is sent verbatim to the
endpoint, so a stray-whitespace env value must not enable the vLLM path with an
invalid URL nor ship a blank model name. These pin the strip/normalize contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings

_RERANK_ENV_KEYS = [
    "RERANK_BASE_URL",
    "RERANK_MODEL",
    "SELF_HOSTED_BASE_URL",
    "DEFAULT_RERANKER_PROVIDER",
    "DEFAULT_USE_RERANK",
    "DEFAULT_RERANKER_MODEL",
]


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


def test_rerank_model_defaults_match_reranker_constants(clean_rerank_env):
    """v0.42 review #25: the rerank default-model literals live in both settings
    (for env override) and reranker_service (as the hard floor). settings.py
    can't import reranker_service without a circular dependency, so this test is
    the single guard that keeps the two in sync instead of a stale comment."""
    from services.reranker_service import (
        DEFAULT_SELF_HOSTED_RERANK_MODEL,
        DEFAULT_VLLM_RERANK_MODEL,
    )

    s = _fresh_settings()
    assert s.rerank_model == DEFAULT_VLLM_RERANK_MODEL
    assert s.self_hosted_rerank_model == DEFAULT_SELF_HOSTED_RERANK_MODEL


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


# ---------------------------------------------------------------------------
# #1572: deployment defaults for new context search configs
# ---------------------------------------------------------------------------


def test_rerank_deployment_defaults_when_unset(clean_rerank_env):
    """Unset DEFAULT_* = today's behaviour: off / voyage / provider default model."""
    s = _fresh_settings()
    assert s.default_reranker_provider == "voyage"
    assert s.default_use_rerank is False
    assert s.default_reranker_model == ""
    assert s.enable_reranking is True


def test_self_hosted_default_on_requires_a_backend(clean_rerank_env):
    """self_hosted + use_rerank=true with neither RERANK_BASE_URL nor
    SELF_HOSTED_BASE_URL would stamp every new context with a reranker that has
    no endpoint — refuse to boot and name the env vars."""
    clean_rerank_env.setenv("DEFAULT_RERANKER_PROVIDER", "self_hosted")
    clean_rerank_env.setenv("DEFAULT_USE_RERANK", "true")
    with pytest.raises(ValidationError, match="RERANK_BASE_URL.*SELF_HOSTED_BASE_URL"):
        _fresh_settings()


def test_self_hosted_default_on_accepts_rerank_base_url(clean_rerank_env):
    clean_rerank_env.setenv("DEFAULT_RERANKER_PROVIDER", "self_hosted")
    clean_rerank_env.setenv("DEFAULT_USE_RERANK", "true")
    clean_rerank_env.setenv("RERANK_BASE_URL", "http://gpu:8002")
    s = _fresh_settings()
    assert s.default_reranker_provider == "self_hosted"
    assert s.default_use_rerank is True


def test_self_hosted_default_on_accepts_self_hosted_base_url_at_default_value(clean_rerank_env):
    """An operator who sets SELF_HOSTED_BASE_URL *to* the default is configured
    (model_fields_set, not a value compare — same idiom as the telemetry probe)."""
    clean_rerank_env.setenv("DEFAULT_RERANKER_PROVIDER", "self_hosted")
    clean_rerank_env.setenv("DEFAULT_USE_RERANK", "true")
    clean_rerank_env.setenv("SELF_HOSTED_BASE_URL", "http://localhost:11434")
    s = _fresh_settings()
    assert s.default_use_rerank is True


def test_self_hosted_default_off_needs_no_backend(clean_rerank_env):
    """Only the ON combination is a boot error; a self_hosted default that is
    off is just a provider preselection."""
    clean_rerank_env.setenv("DEFAULT_RERANKER_PROVIDER", "self_hosted")
    s = _fresh_settings()
    assert s.default_reranker_provider == "self_hosted"
    assert s.default_use_rerank is False


@pytest.mark.parametrize("provider", ["voyage", "cohere"])
def test_remote_default_on_needs_no_key_at_boot(clean_rerank_env, provider):
    """BYOK per workspace decides at runtime (get_active_provider → None →
    un-reranked), so a remote default may be ON without any key configured."""
    clean_rerank_env.setenv("DEFAULT_RERANKER_PROVIDER", provider)
    clean_rerank_env.setenv("DEFAULT_USE_RERANK", "true")
    s = _fresh_settings()
    assert s.default_reranker_provider == provider


def test_unknown_default_provider_rejected(clean_rerank_env):
    clean_rerank_env.setenv("DEFAULT_RERANKER_PROVIDER", "jina")
    with pytest.raises(ValidationError):
        _fresh_settings()


def test_default_reranker_model_whitespace_collapses(clean_rerank_env):
    """Same strip contract as RERANK_MODEL: a blank value means 'provider default'."""
    clean_rerank_env.setenv("DEFAULT_RERANKER_MODEL", "  ")
    assert _fresh_settings().default_reranker_model == ""
    clean_rerank_env.setenv("DEFAULT_RERANKER_MODEL", " bge-reranker-v2-m3 ")
    assert _fresh_settings().default_reranker_model == "bge-reranker-v2-m3"
