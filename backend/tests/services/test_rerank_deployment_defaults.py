"""#1572: one deployment default for every context-search-config default site.

``search_config_defaults(settings)`` turns ``DEFAULT_USE_RERANK`` /
``DEFAULT_RERANKER_PROVIDER`` / ``DEFAULT_RERANKER_MODEL`` into row values;
``default_reranker_model_for`` is the per-provider model map behind it. These
pin (1) unset defaults == today's values, (2) the self_hosted derivation, and
(3) that ``create_context``, ``create_or_get`` and ``reset_to_default`` all
write those values instead of their own literals.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config.settings import Settings
from models.config import ContextSearchConfig
from models.schemas import ContextSearchConfigUpdate
from repositories.config_repository import (
    ContextSearchConfigRepository,
    search_config_defaults,
)
from services.reranker_service import (
    DEFAULT_SELF_HOSTED_RERANK_MODEL,
    DEFAULT_VLLM_RERANK_MODEL,
    default_reranker_model_for,
)

_RERANK_ENV = [
    "RERANK_BASE_URL",
    "RERANK_MODEL",
    "SELF_HOSTED_BASE_URL",
    "SELF_HOSTED_RERANK_MODEL",
    "DEFAULT_RERANKER_PROVIDER",
    "DEFAULT_USE_RERANK",
    "DEFAULT_RERANKER_MODEL",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _RERANK_ENV:
        monkeypatch.delenv(key, raising=False)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# default_reranker_model_for
# ---------------------------------------------------------------------------


class TestDefaultRerankerModelFor:
    def test_remote_providers_use_the_fixed_map_without_settings(self):
        assert default_reranker_model_for("voyage") == "rerank-2"
        assert default_reranker_model_for("cohere") == "rerank-multilingual-v3.0"
        assert default_reranker_model_for("weird") == "rerank-2"

    def test_self_hosted_with_rerank_base_url_is_rerank_model(self):
        s = _settings(rerank_base_url="http://gpu:8002", rerank_model="bge-reranker-v2-m3")
        assert default_reranker_model_for("self_hosted", s) == "bge-reranker-v2-m3"

    def test_self_hosted_without_rerank_base_url_is_self_hosted_rerank_model(self):
        s = _settings(self_hosted_rerank_model="qwen3-reranker:8b")
        assert default_reranker_model_for("self_hosted", s) == "qwen3-reranker:8b"

    def test_self_hosted_blanked_settings_fall_to_the_constant_floors(self):
        s = _settings(rerank_base_url="http://gpu:8002", rerank_model="")
        assert default_reranker_model_for("self_hosted", s) == DEFAULT_VLLM_RERANK_MODEL
        s = _settings(self_hosted_rerank_model="")
        assert default_reranker_model_for("self_hosted", s) == DEFAULT_SELF_HOSTED_RERANK_MODEL

    def test_self_hosted_loads_settings_lazily_when_omitted(self, monkeypatch):
        s = _settings(rerank_base_url="http://gpu:8002", rerank_model="lazy-model")
        monkeypatch.setattr("config.settings.get_settings", lambda: s)
        assert default_reranker_model_for("self_hosted") == "lazy-model"


# ---------------------------------------------------------------------------
# search_config_defaults
# ---------------------------------------------------------------------------


class TestSearchConfigDefaults:
    def test_unset_is_todays_values(self):
        """Acceptance 4: defaults unset → off / voyage / rerank-2 — the ORM
        column defaults (create_context used to write rerank-2-lite; unified)."""
        assert search_config_defaults(_settings()) == {
            "use_rerank": False,
            "reranker_provider": "voyage",
            "reranker_model": "rerank-2",
        }
        col = ContextSearchConfig.__table__.c
        assert col["use_rerank"].default.arg is False
        assert col["reranker_provider"].default.arg == "voyage"
        assert col["reranker_model"].default.arg == "rerank-2"

    def test_self_hosted_default_derives_the_vllm_model(self):
        s = _settings(
            rerank_base_url="http://gpu:8002",
            default_reranker_provider="self_hosted",
            default_use_rerank=True,
        )
        assert search_config_defaults(s) == {
            "use_rerank": True,
            "reranker_provider": "self_hosted",
            "reranker_model": DEFAULT_VLLM_RERANK_MODEL,
        }

    def test_explicit_default_model_wins(self):
        s = _settings(
            default_reranker_provider="cohere", default_reranker_model="rerank-english-v3.0"
        )
        assert search_config_defaults(s)["reranker_model"] == "rerank-english-v3.0"

    def test_values_validate_as_a_search_config_update(self):
        """reset_to_default splats these into ContextSearchConfigUpdate, whose
        model/provider validator must accept every derivable pairing."""
        for s in (
            _settings(),
            _settings(default_reranker_provider="cohere"),
            _settings(rerank_base_url="http://gpu:8002", default_reranker_provider="self_hosted"),
        ):
            ContextSearchConfigUpdate(
                semantic_weight=0.6, bm25_weight=0.4, fetch_factor=3, **search_config_defaults(s)
            )


# ---------------------------------------------------------------------------
# The default-writing sites
# ---------------------------------------------------------------------------


def _self_hosted_default_settings() -> Settings:
    return _settings(
        rerank_base_url="http://gpu:8002",
        rerank_model="qwen3-reranker-0.6b",
        default_reranker_provider="self_hosted",
        default_use_rerank=True,
    )


class TestCreateOrGet:
    async def test_new_row_gets_the_deployment_default(self, monkeypatch):
        monkeypatch.setattr(
            "repositories.config_repository.get_settings", _self_hosted_default_settings
        )
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))
        db.commit = AsyncMock()

        async def _refresh(obj):
            # The ORM Python defaults land on INSERT; stand in for the reload.
            obj.semantic_weight, obj.bm25_weight, obj.fetch_factor = 0.6, 0.4, 3

        db.refresh = _refresh

        row = await ContextSearchConfigRepository(db).create_or_get(uuid4())

        db.add.assert_called_once()
        assert row.use_rerank is True
        assert row.reranker_provider == "self_hosted"
        assert row.reranker_model == "qwen3-reranker-0.6b"

    async def test_existing_row_is_returned_untouched(self, monkeypatch):
        monkeypatch.setattr(
            "repositories.config_repository.get_settings", _self_hosted_default_settings
        )
        existing = MagicMock(spec=ContextSearchConfig)
        existing.semantic_weight = 0.6
        existing.fetch_factor = 3
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: existing))

        row = await ContextSearchConfigRepository(db).create_or_get(uuid4())

        assert row is existing
        db.add.assert_not_called()


class TestResetToDefault:
    async def test_reset_converges_to_the_deployment_default(self, monkeypatch):
        monkeypatch.setattr(
            "repositories.config_repository.get_settings", _self_hosted_default_settings
        )
        repo = ContextSearchConfigRepository(MagicMock())
        captured: dict = {}

        async def _update(context_id, update_data):
            captured["data"] = update_data
            return MagicMock()

        monkeypatch.setattr(repo, "update", _update)
        await repo.reset_to_default(uuid4())

        data = captured["data"]
        assert data.use_rerank is True
        assert data.reranker_provider == "self_hosted"
        assert data.reranker_model == "qwen3-reranker-0.6b"
        # #1207 pins stay explicit (exclude_unset in update()).
        assert data.reinforce_enabled is True

    async def test_reset_with_unset_defaults_is_todays_reset(self, monkeypatch):
        monkeypatch.setattr("repositories.config_repository.get_settings", _settings)
        repo = ContextSearchConfigRepository(MagicMock())
        captured: dict = {}

        async def _update(context_id, update_data):
            captured["data"] = update_data
            return MagicMock()

        monkeypatch.setattr(repo, "update", _update)
        await repo.reset_to_default(uuid4())

        data = captured["data"]
        assert (data.use_rerank, data.reranker_provider, data.reranker_model) == (
            False,
            "voyage",
            "rerank-2",
        )


class TestCreateContext:
    """Acceptance 1 (creation half): with RERANK_BASE_URL + self_hosted +
    DEFAULT_USE_RERANK=true, create_context writes use_rerank=True /
    self_hosted / the vLLM model. Acceptance 4: unset → today's values."""

    async def _create(self, settings: Settings) -> ContextSearchConfig:
        from services.context_service import ContextService

        workspace = MagicMock()
        workspace.plan_name = "basic"
        result = MagicMock()
        result.scalar_one_or_none.return_value = workspace
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        service = ContextService(db=db)

        with (
            patch("services.context_service.get_settings", return_value=settings),
            patch.object(type(service), "validate_context_name", MagicMock(return_value=None)),
            patch.object(
                service, "get_context_by_name_for_workspace", AsyncMock(return_value=None)
            ),
        ):
            await service.create_context(
                workspace_id=uuid4(), name="fresh", is_private=True, create_collection=False
            )

        added = [call.args[0] for call in db.add.call_args_list]
        configs = [obj for obj in added if isinstance(obj, ContextSearchConfig)]
        assert len(configs) == 1, "create_context must stamp exactly one search config"
        return configs[0]

    async def test_self_hosted_default_is_written(self):
        cfg = await self._create(_self_hosted_default_settings())
        assert cfg.use_rerank is True
        assert cfg.reranker_provider == "self_hosted"
        assert cfg.reranker_model == "qwen3-reranker-0.6b"

    async def test_unset_defaults_write_todays_values(self):
        """Deliberate change: the create path wrote rerank-2-lite before #1572;
        it now writes the ORM default rerank-2 like every other site."""
        cfg = await self._create(_settings())
        assert (cfg.use_rerank, cfg.reranker_provider, cfg.reranker_model) == (
            False,
            "voyage",
            "rerank-2",
        )
