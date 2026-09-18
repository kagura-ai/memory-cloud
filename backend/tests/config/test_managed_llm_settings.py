"""Managed-LLM lane + stored-BYOK-resolution settings (#1569).

``MANAGED_LLM_PROVIDER`` / ``MANAGED_LLM_MODEL`` describe the platform-paid
LLM that Memory Analysis and Sleep fall back to when a workspace has no BYOK
key. A half-configured lane must refuse to boot rather than fail at the first
run, so the validators name the missing variable. ``RESOLVE_STORED_BYOK_KEYS``
is the opt-in hardening that makes "BYOK off" also stop *resolving* stored
keys; provisioning on while resolution off is contradictory and is refused.
"""

from __future__ import annotations

import pytest

from config.settings import Settings

_LANE_ENV = [
    "MANAGED_LLM_PROVIDER",
    "MANAGED_LLM_MODEL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "SELF_HOSTED_BASE_URL",
    "RESOLVE_STORED_BYOK_KEYS",
    "ENABLE_BYOK",
    "SELF_HOSTED_LLM_TIMEOUT_SECONDS",
]


@pytest.fixture
def clean_env(monkeypatch):
    for key in _LANE_ENV:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def _fresh(**overrides: object) -> Settings:
    # _env_file=None ignores .env.dev so only os.environ (monkeypatched) is read.
    return Settings(_env_file=None, **overrides)


class TestManagedLlmLane:
    def test_defaults_mean_no_managed_lane(self, clean_env) -> None:
        s = _fresh()
        assert s.managed_llm_provider == ""
        assert s.managed_llm_model == ""
        assert s.resolve_stored_byok_keys is True
        assert s.self_hosted_llm_timeout_seconds == 60.0

    def test_provider_requires_model(self, clean_env) -> None:
        clean_env.setenv("OPENAI_API_KEY", "sk-platform")
        with pytest.raises(ValueError, match="MANAGED_LLM_MODEL"):
            _fresh(managed_llm_provider="openai")

    @pytest.mark.parametrize(
        ("provider", "env_var"),
        [
            ("openai", "OPENAI_API_KEY"),
            ("anthropic", "ANTHROPIC_API_KEY"),
            ("gemini", "GOOGLE_API_KEY"),
        ],
    )
    def test_api_provider_requires_platform_key_env(self, clean_env, provider, env_var) -> None:
        with pytest.raises(ValueError, match=env_var):
            _fresh(managed_llm_provider=provider, managed_llm_model="some-model")
        clean_env.setenv(env_var, "platform-secret")
        s = _fresh(managed_llm_provider=provider, managed_llm_model="some-model")
        assert (s.managed_llm_provider, s.managed_llm_model) == (provider, "some-model")

    def test_self_hosted_requires_explicit_base_url(self, clean_env) -> None:
        # The default base URL does not count — the operator must point the
        # lane at a backend deliberately (same idiom as the telemetry probe).
        with pytest.raises(ValueError, match="SELF_HOSTED_BASE_URL"):
            _fresh(managed_llm_provider="self_hosted", managed_llm_model="qwen3:8b")
        clean_env.setenv("SELF_HOSTED_BASE_URL", "http://vllm:8000")
        s = _fresh(managed_llm_provider="self_hosted", managed_llm_model="qwen3:8b")
        assert s.managed_llm_provider == "self_hosted"

    def test_unknown_provider_refused(self, clean_env) -> None:
        with pytest.raises(ValueError):
            _fresh(managed_llm_provider="ollama_cloud", managed_llm_model="x")

    def test_timeout_from_env(self, clean_env) -> None:
        clean_env.setenv("SELF_HOSTED_LLM_TIMEOUT_SECONDS", "120")
        assert _fresh().self_hosted_llm_timeout_seconds == 120.0
        with pytest.raises(ValueError):
            _fresh(self_hosted_llm_timeout_seconds=0)


class TestResolveStoredByokKeys:
    def test_off_requires_byok_provisioning_off(self, clean_env) -> None:
        with pytest.raises(ValueError, match="RESOLVE_STORED_BYOK_KEYS"):
            _fresh(resolve_stored_byok_keys=False)  # enable_byok defaults True
        s = _fresh(resolve_stored_byok_keys=False, enable_byok=False)
        assert s.resolve_stored_byok_keys is False

    def test_default_on_with_byok_off_is_fine(self, clean_env) -> None:
        # #1167 posture unchanged: BYOK off leaves resolution untouched.
        s = _fresh(enable_byok=False)
        assert s.resolve_stored_byok_keys is True
