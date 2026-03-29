"""Tests for MCP update_search_config validation.

Issue #25: Validates that MCP tool reuses ContextSearchConfigUpdate Pydantic model
for consistent validation with REST API.
"""

import pytest
from pydantic import ValidationError

from models.schemas import ContextSearchConfigUpdate


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
