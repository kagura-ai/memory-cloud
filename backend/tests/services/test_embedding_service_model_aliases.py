"""The wire model id for self-hosted embedding requests (#1525).

``EmbeddingService.model`` is the registry name and must stay that way — the
collection name, the allowlist and the Redis cache key all hang off it. Only
the kwargs handed to ``embeddings.create`` may carry the upstream id.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from services.embedding_service import EmbeddingService


def _settings(**overrides):
    settings = MagicMock()
    settings.embedding_model = "text-embedding-3-small"
    settings.embedding_dimensions = 512
    settings.embedding_provider = "openai"
    settings.self_hosted_base_url = "http://inference:8000"
    settings.self_hosted_api_key = ""
    settings.self_hosted_model_aliases = ""
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _service(model: str, **settings_overrides) -> EmbeddingService:
    with patch("config.settings.get_settings", return_value=_settings(**settings_overrides)):
        return EmbeddingService(AsyncMock(), model=model)


class TestSelfHostedAliases:
    def test_alias_changes_only_the_wire_model(self):
        service = _service(
            "qwen3-embedding:4b",
            self_hosted_model_aliases="qwen3-embedding:4b=Qwen/Qwen3-Embedding-4B",
        )
        assert service.provider == "self_hosted"
        kwargs = service._build_embedding_kwargs(["hello"])
        assert kwargs == {"model": "Qwen/Qwen3-Embedding-4B", "input": ["hello"]}
        # The identity the rest of the system sees is untouched.
        assert service.model == "qwen3-embedding:4b"
        assert service.dimensions == 2560

    def test_no_alias_sends_the_registry_name(self):
        service = _service("qwen3-embedding:4b")
        assert service._build_embedding_kwargs("x") == {"model": "qwen3-embedding:4b", "input": "x"}

    def test_aliases_never_apply_to_openai(self):
        # An operator who aliases an OpenAI model by mistake must not change
        # what is sent to OpenAI (and `dimensions` still rides along).
        service = _service(
            "text-embedding-3-small",
            self_hosted_model_aliases="text-embedding-3-small=something-else",
        )
        assert service.provider == "openai"
        assert service._build_embedding_kwargs("x") == {
            "model": "text-embedding-3-small",
            "input": "x",
            "dimensions": 512,
        }
