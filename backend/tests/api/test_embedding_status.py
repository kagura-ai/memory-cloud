"""Tests for embedding queue status endpoints (Issue #93)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.workspace import get_embedding_status


class TestGetEmbeddingStatus:
    """Test GET /workspace/embedding-status endpoint."""

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def mock_user(self):
        return {
            "user_id": "test_user_123",
            "current_workspace_id": str(uuid4()),
        }

    @pytest.mark.asyncio
    async def test_returns_counts_by_status(self, mock_db, mock_user):
        """Test that endpoint returns correct status counts."""
        # Mock GROUP BY result: 2 statuses
        mock_status_result = MagicMock()
        mock_status_result.all.return_value = [
            ("success", 100),
            ("pending", 5),
            ("failed", 0),
        ]
        mock_db.execute.return_value = mock_status_result

        response = await get_embedding_status(user=mock_user, db=mock_db, context_id=None)

        assert response.total == 105
        assert response.by_status["success"] == 100
        assert response.by_status["pending"] == 5
        assert response.failed_memories == []

    @pytest.mark.asyncio
    async def test_no_workspace_raises_400(self, mock_db):
        """Test that missing workspace_id returns 400."""
        user = {"user_id": "test_user_123"}

        with pytest.raises(HTTPException) as exc:
            await get_embedding_status(user=user, db=mock_db, context_id=None)

        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_invalid_context_id_raises_400(self, mock_db, mock_user):
        """Test that invalid UUID context_id returns 400."""
        with pytest.raises(HTTPException) as exc:
            await get_embedding_status(user=mock_user, db=mock_db, context_id="not-a-uuid")

        assert exc.value.status_code == 400
        assert "Invalid context_id" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_includes_failed_details(self, mock_db, mock_user):
        """Test that failed memories include details."""
        mem_id = uuid4()
        mock_mem = MagicMock()
        mock_mem.id = mem_id
        mock_mem.summary = "Test memory summary"
        mock_mem.embedding_error = "Model not available"
        mock_mem.created_at = datetime(2026, 4, 1, 10, 0, 0)
        mock_mem.updated_at = datetime(2026, 4, 1, 12, 0, 0)

        # First call: GROUP BY status counts
        mock_status_result = MagicMock()
        mock_status_result.all.return_value = [("failed", 1)]

        # Second call: failed memory details
        mock_failed_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_mem]
        mock_failed_result.scalars.return_value = mock_scalars

        mock_db.execute.side_effect = [mock_status_result, mock_failed_result]

        response = await get_embedding_status(user=mock_user, db=mock_db, context_id=None)

        assert response.total == 1
        assert len(response.failed_memories) == 1
        assert response.failed_memories[0].id == str(mem_id)
        assert response.failed_memories[0].embedding_error == "Model not available"
