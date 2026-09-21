"""When a stored external key is protected (#1613).

``PROTECTED_KEYS`` used to be the whole rule: ``OPENAI_API_KEY`` could never be
deleted or disabled. These pin the rule that replaced it — the key is protected
only while something would actually read it:

- BYOK provisioning AND stored-key resolution are both on, and
- the deployment embeds with OpenAI, or the workspace routes at least one live
  context to an OpenAI embedding model.

The workspace half (live vs. soft-deleted contexts, contexts without a config
row) runs against a real database in
``tests/integration/test_external_key_protection_db.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from config.constants import EMBEDDING_MODEL_REGISTRY
from config.plan_tiers import PROTECTED_KEYS
from config.settings import Settings
from services.context_routing import _LEGACY_MODEL
from services.embedding_service import EmbeddingService
from services.external_key_protection import (
    _models_of_a_context_without_config,
    count_openai_routed_contexts,
    embedding_provider_of,
    evaluate_key_protection,
    is_key_protected,
    is_protection_candidate,
)

_FLAG_ENV = [
    "ENABLE_BYOK",
    "RESOLVE_STORED_BYOK_KEYS",
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _FLAG_ENV:
        monkeypatch.delenv(key, raising=False)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


_SELF_HOSTED = {"embedding_provider": "self_hosted", "embedding_model": "qwen3-embedding:0.6b"}


class TestIsKeyProtected:
    def test_openai_embedding_provider_protects_the_key(self):
        settings = _settings()  # defaults: EMBEDDING_PROVIDER=openai, BYOK on
        assert is_key_protected(
            "OPENAI_API_KEY", settings, workspace_routes_to_openai_embeddings=False
        )

    def test_self_hosted_provider_without_openai_contexts_is_deletable(self):
        settings = _settings(**_SELF_HOSTED)
        assert not is_key_protected(
            "OPENAI_API_KEY", settings, workspace_routes_to_openai_embeddings=False
        )

    def test_self_hosted_provider_with_an_openai_routed_context_is_protected(self):
        settings = _settings(**_SELF_HOSTED)
        assert is_key_protected(
            "OPENAI_API_KEY", settings, workspace_routes_to_openai_embeddings=True
        )

    def test_byok_off_makes_the_key_deletable(self):
        """Owners must be able to withdraw a credential they can no longer manage."""
        settings = _settings(enable_byok=False)
        assert settings.embedding_provider == "openai"
        assert not is_key_protected(
            "OPENAI_API_KEY", settings, workspace_routes_to_openai_embeddings=True
        )

    def test_stored_key_resolution_off_makes_the_key_deletable(self):
        settings = _settings(enable_byok=False, resolve_stored_byok_keys=False)
        assert not is_key_protected(
            "OPENAI_API_KEY", settings, workspace_routes_to_openai_embeddings=True
        )

    def test_resolution_flag_is_checked_on_its_own(self):
        """Not only via ENABLE_BYOK: the boot validator couples the two flags,
        the predicate must not rely on it."""
        settings = _settings().model_copy(update={"resolve_stored_byok_keys": False})
        assert settings.enable_byok is True
        assert not is_key_protected(
            "OPENAI_API_KEY", settings, workspace_routes_to_openai_embeddings=True
        )

    @pytest.mark.parametrize(
        "key_name", ["COHERE_API_KEY", "VOYAGE_API_KEY", "ANTHROPIC_API_KEY", "MY_OPENAI_KEY"]
    )
    def test_non_candidate_key_is_never_protected(self, key_name):
        assert key_name not in PROTECTED_KEYS
        assert not is_key_protected(
            key_name, _settings(), workspace_routes_to_openai_embeddings=True
        )

    def test_candidate_precheck_matches_the_flag_half_of_the_rule(self):
        assert is_protection_candidate("OPENAI_API_KEY", _settings())
        assert not is_protection_candidate("OPENAI_API_KEY", _settings(enable_byok=False))
        assert not is_protection_candidate("COHERE_API_KEY", _settings())


class TestEmbeddingProviderOf:
    def test_registry_models_resolve_like_the_embedding_service(self):
        """Drift guard: the predicate and ``EmbeddingService`` must agree on
        which provider serves a model, whatever EMBEDDING_PROVIDER says."""
        settings = _settings(**_SELF_HOSTED)
        for model in EMBEDDING_MODEL_REGISTRY:
            assert (
                embedding_provider_of(model, settings)
                == EmbeddingService(AsyncMock(), model=model).provider
            )

    def test_unknown_model_falls_back_to_the_deployment_provider(self):
        assert embedding_provider_of("not-in-registry", _settings()) == "openai"
        assert embedding_provider_of("not-in-registry", _settings(**_SELF_HOSTED)) == (
            "self_hosted"
        )


class TestContextWithoutConfigRow:
    """A legacy context with no ``ContextSearchConfig`` row.

    Its next recall materialises the row with the column default, an OpenAI
    model, so it counts whatever the deployment embeds with — otherwise the key
    would be deletable right up to the recall that starts needing it.
    """

    @staticmethod
    def _db(*groups: tuple[str | None, int]) -> AsyncMock:
        """A session whose grouped query answers ``(embedding_model, count)`` rows."""
        db = AsyncMock()
        db.execute.return_value = MagicMock(all=MagicMock(return_value=list(groups)))
        return db

    @pytest.mark.asyncio
    async def test_counts_on_a_self_hosted_deployment(self):
        db = self._db((None, 2), ("qwen3-embedding:0.6b", 5))
        assert await count_openai_routed_contexts(db, uuid4(), _settings(**_SELF_HOSTED)) == 2

    @pytest.mark.asyncio
    async def test_counts_once_on_an_openai_deployment(self):
        db = self._db((None, 2))
        assert await count_openai_routed_contexts(db, uuid4(), _settings()) == 2

    def test_lazy_row_model_is_the_legacy_routing_model(self):
        """Drift guard: the column default read here is the model
        ``resolve_context_embedding`` already reports for such a context."""
        assert _models_of_a_context_without_config(_settings(**_SELF_HOSTED)) == (
            _SELF_HOSTED["embedding_model"],
            _LEGACY_MODEL,
        )


class TestEvaluateKeyProtection:
    """The route-facing wrapper: skips the workspace query when it cannot matter."""

    @pytest.mark.asyncio
    async def test_non_candidate_never_queries(self):
        db = AsyncMock()
        protection = await evaluate_key_protection(
            db, key_name="COHERE_API_KEY", workspace_id=uuid4(), settings=_settings()
        )
        assert protection.protected is False
        assert protection.reason is None
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_byok_off_never_queries(self):
        db = AsyncMock()
        protection = await evaluate_key_protection(
            db,
            key_name="OPENAI_API_KEY",
            workspace_id=uuid4(),
            settings=_settings(enable_byok=False),
        )
        assert protection.protected is False
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_openai_deployment_names_the_deployment_without_querying(self):
        db = AsyncMock()
        protection = await evaluate_key_protection(
            db, key_name="OPENAI_API_KEY", workspace_id=uuid4(), settings=_settings()
        )
        assert protection.protected is True
        assert "this deployment" in (protection.reason or "")
        db.execute.assert_not_called()
