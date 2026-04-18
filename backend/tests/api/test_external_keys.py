"""Tests for External API Keys management (Issue #105).

Tests:
- CRUD operations
- Toggle enabled/disabled state
- Exclusive reranker validation (Cohere/Voyage)
- OpenAI cannot be disabled
- Context scoping
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.routes.external_keys import (
    EMBEDDING_PROVIDERS,
    RERANKER_PROVIDERS,
    validate_reranker_exclusivity,
)


class TestRerankerExclusivity:
    """Test reranker exclusivity validation (Issue #105)."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        return AsyncMock()

    @pytest.fixture
    def workspace_id(self):
        """Test workspace UUID — Issue #385: reranker exclusivity is workspace-scoped."""
        from uuid import UUID

        return UUID("00000000-0000-0000-0000-000000000001")

    @pytest.mark.asyncio
    async def test_openai_cannot_be_disabled(self, mock_db, workspace_id):
        """Test that OpenAI keys cannot be disabled."""
        with pytest.raises(HTTPException) as exc_info:
            await validate_reranker_exclusivity(
                db=mock_db,
                workspace_id=workspace_id,
                provider="openai",
                enabled=False,  # Trying to disable
            )

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error"] == "cannot_disable_embeddings"

    @pytest.mark.asyncio
    async def test_openai_enable_always_allowed(self, mock_db, workspace_id):
        """Test that enabling OpenAI is always allowed."""
        # Should not raise any exception
        await validate_reranker_exclusivity(
            db=mock_db,
            workspace_id=workspace_id,
            provider="openai",
            enabled=True,
        )

    @pytest.mark.asyncio
    async def test_disable_reranker_allowed(self, mock_db, workspace_id):
        """Test that disabling a reranker is always allowed."""
        # Should not raise - disabling is fine
        await validate_reranker_exclusivity(
            db=mock_db,
            workspace_id=workspace_id,
            provider="cohere",
            enabled=False,
        )

    @pytest.mark.asyncio
    async def test_enable_reranker_with_no_conflict(self, mock_db, workspace_id):
        """Test enabling reranker when no other reranker is enabled."""
        # Mock DB query returning no conflicting key
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        # Should not raise
        await validate_reranker_exclusivity(
            db=mock_db,
            workspace_id=workspace_id,
            provider="cohere",
            enabled=True,
        )

    @pytest.mark.asyncio
    async def test_enable_cohere_when_voyage_enabled(self, mock_db, workspace_id):
        """Test that enabling Cohere fails when Voyage is already enabled."""
        # Mock existing Voyage key
        mock_voyage_key = MagicMock()
        mock_voyage_key.provider = "voyage"
        mock_voyage_key.key_name = "VOYAGE_API_KEY"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_voyage_key
        mock_db.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await validate_reranker_exclusivity(
                db=mock_db,
                workspace_id=workspace_id,
                provider="cohere",
                enabled=True,
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["error"] == "reranker_provider_conflict"
        assert exc_info.value.detail["conflicting_provider"] == "voyage"

    @pytest.mark.asyncio
    async def test_enable_voyage_when_cohere_enabled(self, mock_db, workspace_id):
        """Test that enabling Voyage fails when Cohere is already enabled."""
        # Mock existing Cohere key
        mock_cohere_key = MagicMock()
        mock_cohere_key.provider = "cohere"
        mock_cohere_key.key_name = "COHERE_API_KEY"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_cohere_key
        mock_db.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await validate_reranker_exclusivity(
                db=mock_db,
                workspace_id=workspace_id,
                provider="voyage",
                enabled=True,
            )

        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["error"] == "reranker_provider_conflict"
        assert exc_info.value.detail["conflicting_provider"] == "cohere"

    @pytest.mark.asyncio
    async def test_no_conflict_check(self, mock_db, workspace_id):
        """Test enabling reranker with no existing conflict."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        await validate_reranker_exclusivity(
            db=mock_db,
            workspace_id=workspace_id,
            provider="cohere",
            enabled=True,
        )

        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_exclude_self_when_updating(self, mock_db, workspace_id):
        """Test that updating a key excludes itself from conflict check."""
        existing_key_id = 123

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        await validate_reranker_exclusivity(
            db=mock_db,
            workspace_id=workspace_id,
            provider="cohere",
            enabled=True,
            exclude_key_id=existing_key_id,
        )

        # Should complete without error
        mock_db.execute.assert_called_once()


class TestProviderConstants:
    """Test provider constant definitions."""

    def test_reranker_providers(self):
        """Test RERANKER_PROVIDERS contains expected values."""
        assert "cohere" in RERANKER_PROVIDERS
        assert "voyage" in RERANKER_PROVIDERS
        assert "openai" not in RERANKER_PROVIDERS

    def test_embedding_providers(self):
        """Test EMBEDDING_PROVIDERS contains expected values."""
        assert "openai" in EMBEDDING_PROVIDERS
        assert "cohere" not in EMBEDDING_PROVIDERS
        assert "voyage" not in EMBEDDING_PROVIDERS
