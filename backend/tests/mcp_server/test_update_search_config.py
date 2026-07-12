"""Tests for MCP update_search_config validation.

Issue #25: Validates that MCP tool reuses ContextSearchConfigUpdate Pydantic model
for consistent validation with REST API.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from models.schemas import ContextSearchConfigUpdate
from repositories.config_repository import ContextSearchConfigRepository


class TestSearchConfigValidation:
    """Test ContextSearchConfigUpdate validation (shared by REST + MCP)."""

    def test_valid_config(self):
        config = ContextSearchConfigUpdate(
            semantic_weight=0.6,
            bm25_weight=0.4,
            fetch_factor=3,
            use_rerank=False,
            reranker_provider="voyage",
            reranker_model="rerank-2",
        )
        assert float(config.semantic_weight) == 0.6

    def test_weights_must_sum_to_one(self):
        with pytest.raises(ValidationError, match="sum to 1.0"):
            ContextSearchConfigUpdate(
                semantic_weight=0.8,
                bm25_weight=0.5,
                fetch_factor=3,
                use_rerank=False,
                reranker_provider="voyage",
                reranker_model="rerank-2",
            )

    def test_weight_out_of_range(self):
        with pytest.raises(ValidationError):
            ContextSearchConfigUpdate(
                semantic_weight=1.5,
                bm25_weight=-0.5,
                fetch_factor=3,
                use_rerank=False,
                reranker_provider="voyage",
                reranker_model="rerank-2",
            )

    def test_fetch_factor_out_of_range(self):
        with pytest.raises(ValidationError):
            ContextSearchConfigUpdate(
                semantic_weight=0.6,
                bm25_weight=0.4,
                fetch_factor=20,
                use_rerank=False,
                reranker_provider="voyage",
                reranker_model="rerank-2",
            )

    def test_routing_mode_default_off(self):
        """#1212: omitting routing_mode leaves it at 'off' (and unset, so the
        repository's exclude_unset update does not touch the column)."""
        config = ContextSearchConfigUpdate(
            semantic_weight=0.6,
            bm25_weight=0.4,
            fetch_factor=3,
            use_rerank=False,
            reranker_provider="voyage",
            reranker_model="rerank-2",
        )
        assert config.routing_mode == "off"
        assert "routing_mode" not in config.model_dump(exclude_unset=True)

    def test_routing_mode_accepts_all_gate_values(self):
        for mode in ("off", "log_only", "active"):
            config = ContextSearchConfigUpdate(
                semantic_weight=0.6,
                bm25_weight=0.4,
                fetch_factor=3,
                use_rerank=False,
                reranker_provider="voyage",
                reranker_model="rerank-2",
                routing_mode=mode,
            )
            assert config.routing_mode == mode

    def test_routing_mode_rejects_unknown_value(self):
        with pytest.raises(ValidationError):
            ContextSearchConfigUpdate(
                semantic_weight=0.6,
                bm25_weight=0.4,
                fetch_factor=3,
                use_rerank=False,
                reranker_provider="voyage",
                reranker_model="rerank-2",
                routing_mode="always",
            )

    def test_invalid_reranker_model_for_provider(self):
        with pytest.raises(ValidationError, match="Invalid model"):
            ContextSearchConfigUpdate(
                semantic_weight=0.6,
                bm25_weight=0.4,
                fetch_factor=3,
                use_rerank=True,
                reranker_provider="voyage",
                reranker_model="rerank-multilingual-v3.0",  # cohere model for voyage
            )

    def test_fifty_fifty_weights(self):
        config = ContextSearchConfigUpdate(
            semantic_weight=0.5,
            bm25_weight=0.5,
            fetch_factor=3,
            use_rerank=False,
            reranker_provider="voyage",
            reranker_model="rerank-2",
        )
        assert float(config.semantic_weight) == 0.5
        assert float(config.bm25_weight) == 0.5


class TestPartialUpdatePreservesRoutingMode:
    """Gate2/QA (#1212): a partial update that omits routing_mode must not
    reset an opted-in context back to 'off' — the repository applies
    model_dump(exclude_unset=True), pinned here through the real update()."""

    @pytest.mark.asyncio
    async def test_partial_update_keeps_active(self) -> None:
        config = SimpleNamespace(routing_mode="active", semantic_weight=0.6)
        repo = ContextSearchConfigRepository(AsyncMock())
        repo.get_by_context = AsyncMock(return_value=config)

        await repo.update(
            uuid4(),
            ContextSearchConfigUpdate(
                semantic_weight=0.7,
                bm25_weight=0.3,
                fetch_factor=3,
                use_rerank=False,
                reranker_provider="voyage",
                reranker_model="rerank-2",
            ),
        )
        assert config.routing_mode == "active"
        assert float(config.semantic_weight) == 0.7

    @pytest.mark.asyncio
    async def test_explicit_routing_mode_is_applied(self) -> None:
        config = SimpleNamespace(routing_mode="active")
        repo = ContextSearchConfigRepository(AsyncMock())
        repo.get_by_context = AsyncMock(return_value=config)

        await repo.update(
            uuid4(),
            ContextSearchConfigUpdate(
                semantic_weight=0.6,
                bm25_weight=0.4,
                fetch_factor=3,
                use_rerank=False,
                reranker_provider="voyage",
                reranker_model="rerank-2",
                routing_mode="off",
            ),
        )
        assert config.routing_mode == "off"
