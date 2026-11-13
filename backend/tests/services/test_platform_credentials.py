"""The probe's credential module reaches platform credentials only (Issue #2768).

Structural guards: ``services/platform_credentials.py`` and the probe module
import none of the services that resolve a workspace's own key, and no
function of the credential module can take a session, workspace, context or
user. Behavioural checks: what each function resolves, and that the SDK
clients have their retries off.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from config.settings import Settings
from services import platform_credentials as pc
from services.llm_providers import AnthropicProvider, OpenAIProvider, SelfHostedProvider
from services.reranker_service import SelfHostedReranker, VLLMReranker

SRC = Path(__file__).resolve().parents[2] / "src"
GUARDED = [
    SRC / "services" / "platform_credentials.py",
    SRC / "services" / "operations" / "probe.py",
]

# Modules that resolve a workspace's stored key or hold the stored-key model.
FORBIDDEN_MODULES = {
    "services.llm_service",
    "services.embedding_service",
    "services.byok_resolution",
    "models.secrets",
    "repositories.config_repository",
}
FORBIDDEN_NAMES = {
    "LLMService",
    "EmbeddingService",
    "RerankerService",
    "ExternalAPIKey",
    "_resolve_api_key",
    "_get_user_api_key",
    "get_active_provider",
}
# The only names the probe side may take from the reranker module.
RERANKER_ALLOWED = {
    "SelfHostedReranker",
    "VLLMReranker",
    "RerankerProvider",
    "DEFAULT_SELF_HOSTED_RERANK_MODEL",
    "DEFAULT_VLLM_RERANK_MODEL",
}
FORBIDDEN_PARAMS = {"db", "session", "workspace_id", "context_id", "user_id"}


def _settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("path", GUARDED, ids=lambda p: p.name)
def test_no_import_or_call_reaches_a_workspace_key(path: Path) -> None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in FORBIDDEN_MODULES, alias.name
                assert alias.name != "services.reranker_service", "import names, not the module"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module not in FORBIDDEN_MODULES, module
            names = {alias.name for alias in node.names}
            assert not names & FORBIDDEN_NAMES, names & FORBIDDEN_NAMES
            if module == "services.reranker_service":
                assert names <= RERANKER_ALLOWED, names - RERANKER_ALLOWED
            if module == "services":
                assert not names & {"llm_service", "embedding_service", "reranker_service"}
        elif isinstance(node, ast.Attribute):
            assert node.attr not in FORBIDDEN_NAMES, node.attr
        elif isinstance(node, ast.Name):
            assert node.id not in FORBIDDEN_NAMES, node.id


def test_no_credential_function_takes_a_session_or_an_identity() -> None:
    functions = [
        obj
        for name, obj in vars(pc).items()
        if inspect.isfunction(obj) and obj.__module__ == pc.__name__
    ]
    assert len(functions) >= 6
    for fn in functions:
        params = set(inspect.signature(fn).parameters)
        assert not params & FORBIDDEN_PARAMS, (fn.__name__, params & FORBIDDEN_PARAMS)


def test_the_key_env_map_covers_the_managed_lane_map() -> None:
    from config.settings import MANAGED_LLM_PLATFORM_KEY_ENV

    assert MANAGED_LLM_PLATFORM_KEY_ENV.items() <= pc.PLATFORM_LLM_KEY_ENV.items()
    assert pc.PLATFORM_LLM_KEY_ENV["ollama_cloud"] == "OLLAMA_API_KEY"


class TestCredential:
    def test_api_key_provider_reads_its_env(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform-anthropic")
        assert pc.platform_llm_credential("anthropic", _settings()) == "sk-platform-anthropic"

    def test_missing_env_is_none(self, monkeypatch) -> None:
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        assert pc.platform_llm_credential("gemini", _settings()) is None

    def test_unknown_provider_never_falls_back_to_another_key(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-platform-openai")
        assert pc.platform_llm_credential("mystery", _settings()) is None
        assert pc.platform_llm_provider("mystery", _settings()) is None

    def test_self_hosted_env_then_setting(self, monkeypatch) -> None:
        monkeypatch.delenv("SELF_HOSTED_BASE_URL", raising=False)
        settings = _settings(self_hosted_base_url="http://inference.example.com:8000")
        assert pc.platform_llm_credential("self_hosted", settings) == (
            "http://inference.example.com:8000"
        )
        monkeypatch.setenv("SELF_HOSTED_BASE_URL", "http://other.example.com")
        assert pc.platform_llm_credential("self_hosted", settings) == "http://other.example.com"


class TestProviders:
    def test_openai_client_has_retries_off(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-platform-openai")
        provider = pc.platform_llm_provider("openai", _settings())
        assert isinstance(provider, OpenAIProvider)
        assert provider._client.max_retries == 0
        assert provider._client.api_key == "sk-platform-openai"

    def test_anthropic_client_has_retries_off(self, monkeypatch) -> None:
        pytest.importorskip("anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-platform-anthropic")
        provider = pc.platform_llm_provider("anthropic", _settings())
        assert isinstance(provider, AnthropicProvider)
        assert provider._client is not None
        assert provider._client.max_retries == 0

    def test_self_hosted_provider_on_the_platform_base_url(self, monkeypatch) -> None:
        monkeypatch.delenv("SELF_HOSTED_BASE_URL", raising=False)
        provider = pc.platform_llm_provider(
            "self_hosted", _settings(self_hosted_base_url="http://inference.example.com")
        )
        assert isinstance(provider, SelfHostedProvider)
        assert provider._base_url == "http://inference.example.com"
        assert provider._client.max_retries == 0

    def test_no_credential_no_provider(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert pc.platform_llm_provider("openai", _settings()) is None


class TestEmbedding:
    def test_registry_model_decides_provider_and_dimensions(self) -> None:
        lane = pc.embedding_lane(
            _settings(embedding_model="qwen3-embedding:0.6b", embedding_provider="openai")
        )
        assert (lane.provider, lane.model, lane.dimensions) == (
            "self_hosted",
            "qwen3-embedding:0.6b",
            1024,
        )

    def test_openai_client(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-platform-openai")
        emb = pc.platform_embedding_client(_settings(embedding_model="text-embedding-3-small"))
        assert emb is not None
        assert emb.client.max_retries == 0
        assert emb.client.api_key == "sk-platform-openai"
        assert emb.wire_model == "text-embedding-3-small"
        assert emb.extra == {"dimensions": 512}

    def test_openai_without_key_is_none(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        settings = _settings(embedding_model="text-embedding-3-small")
        assert pc.platform_embedding_client(settings) is None
        assert pc.embedding_credential_present(settings) is False

    def test_self_hosted_client_applies_aliases(self) -> None:
        emb = pc.platform_embedding_client(
            _settings(
                embedding_model="qwen3-embedding:4b",
                self_hosted_base_url="http://inference.example.com",
                self_hosted_model_aliases="qwen3-embedding:4b=Qwen/Qwen3-Embedding-4B",
            )
        )
        assert emb is not None
        assert str(emb.client.base_url).startswith("http://inference.example.com/v1")
        assert emb.wire_model == "Qwen/Qwen3-Embedding-4B"
        assert emb.extra == {}
        assert emb.client.max_retries == 0


class TestRerank:
    def test_off_without_reranking(self) -> None:
        settings = _settings(enable_reranking=False, rerank_base_url="http://rr.example.com")
        assert pc.rerank_lane(settings) is None
        assert pc.platform_reranker(settings) is None

    def test_remote_default_has_no_platform_lane(self) -> None:
        assert pc.rerank_lane(_settings(default_reranker_provider="voyage")) is None

    def test_rerank_endpoint(self) -> None:
        settings = _settings(rerank_base_url="http://rr.example.com", rerank_model="rr-small")
        assert pc.rerank_lane(settings) == pc.RerankLane(provider="self_hosted", model="rr-small")
        assert isinstance(pc.platform_reranker(settings), VLLMReranker)

    def test_self_hosted_default(self) -> None:
        settings = _settings(
            default_reranker_provider="self_hosted",
            self_hosted_base_url="http://inference.example.com",
            self_hosted_rerank_model="rr-local",
        )
        assert pc.rerank_lane(settings) == pc.RerankLane(provider="self_hosted", model="rr-local")
        reranker = pc.platform_reranker(settings)
        assert isinstance(reranker, SelfHostedReranker)
        assert reranker.base_url == "http://inference.example.com"
