"""Deployment-level embedding-model allowlist (#1517)."""

from config.constants import EMBEDDING_MODEL_REGISTRY
from config.embedding_policy import allowed_embedding_models, is_embedding_model_allowed


class TestAllowedEmbeddingModels:
    def test_empty_setting_offers_the_whole_registry(self):
        # The default must not change any existing deployment's behaviour.
        assert allowed_embedding_models(None) == tuple(EMBEDDING_MODEL_REGISTRY)
        assert allowed_embedding_models("") == tuple(EMBEDDING_MODEL_REGISTRY)
        assert allowed_embedding_models("   ") == tuple(EMBEDDING_MODEL_REGISTRY)

    def test_a_configured_list_narrows_the_offer(self):
        assert allowed_embedding_models("qwen3-embedding:4b") == ("qwen3-embedding:4b",)

    def test_whitespace_and_blank_entries_are_tolerated(self):
        assert allowed_embedding_models(" qwen3-embedding:4b , ,text-embedding-3-small ") == (
            "text-embedding-3-small",
            "qwen3-embedding:4b",
        )

    def test_order_follows_the_registry_not_the_setting(self):
        # A stable order keeps the /system/embedding/models listing from
        # reshuffling because an operator retyped their env var.
        assert allowed_embedding_models(
            "qwen3-embedding:4b,text-embedding-3-small"
        ) == allowed_embedding_models("text-embedding-3-small,qwen3-embedding:4b")

    def test_unknown_names_in_the_setting_are_dropped(self):
        # A typo must not conjure a model the code cannot size or route.
        assert allowed_embedding_models("not-a-real-model") == ()
        assert allowed_embedding_models("not-a-real-model,qwen3-embedding:4b") == (
            "qwen3-embedding:4b",
        )


class TestIsEmbeddingModelAllowed:
    def test_default_allows_every_registry_model(self):
        for name in EMBEDDING_MODEL_REGISTRY:
            assert is_embedding_model_allowed(name, None) is True

    def test_a_narrowed_deployment_refuses_the_rest(self):
        assert is_embedding_model_allowed("qwen3-embedding:4b", "qwen3-embedding:4b") is True
        assert is_embedding_model_allowed("text-embedding-3-large", "qwen3-embedding:4b") is False

    def test_a_model_outside_the_registry_is_never_allowed(self):
        assert is_embedding_model_allowed("not-a-real-model", None) is False
        assert is_embedding_model_allowed("not-a-real-model", "not-a-real-model") is False
