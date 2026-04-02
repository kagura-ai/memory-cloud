"""Tests for WorkspaceService.get_collection_memory_stats (Issue #65).

Verifies single-query optimization for private/shared context stats.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.workspace_service import WorkspaceService


class TestGetCollectionMemoryStats:
    """Test get_collection_memory_stats with single-query optimization."""

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def service(self, mock_db):
        return WorkspaceService(mock_db)

    @pytest.fixture
    def user_id(self):
        return "test_user_123"

    def _make_context(self, is_private=False, created_by="test_user_123"):
        ctx = MagicMock()
        ctx.id = uuid4()
        ctx.is_private = is_private
        ctx.created_by = created_by
        return ctx

    @pytest.mark.asyncio
    async def test_both_private_and_shared_single_query(self, service, mock_db, user_id):
        """Issue #65: Both private and shared contexts handled in one query."""
        private_ctx = self._make_context(is_private=True, created_by=user_id)
        shared_ctx = self._make_context(is_private=False)

        private_row = MagicMock(context_id=private_ctx.id, memory_count=5)
        shared_row = MagicMock(context_id=shared_ctx.id, memory_count=10)

        mock_result = MagicMock()
        mock_result.all.return_value = [private_row, shared_row]
        mock_db.execute.return_value = mock_result

        result = await service.get_collection_memory_stats(
            user_id=user_id,
            contexts=[private_ctx, shared_ctx],
            is_workspace_owner=False,
        )

        assert result[str(private_ctx.id)] == (5, 0)
        assert result[str(shared_ctx.id)] == (10, 0)
        assert mock_db.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_only_shared_contexts(self, service, mock_db, user_id):
        """Only shared contexts — single query."""
        shared_ctx = self._make_context(is_private=False)
        shared_row = MagicMock(context_id=shared_ctx.id, memory_count=7)

        mock_result = MagicMock()
        mock_result.all.return_value = [shared_row]
        mock_db.execute.return_value = mock_result

        result = await service.get_collection_memory_stats(
            user_id=user_id,
            contexts=[shared_ctx],
            is_workspace_owner=False,
        )

        assert result[str(shared_ctx.id)] == (7, 0)
        assert mock_db.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_empty_contexts(self, service, mock_db, user_id):
        """No contexts — no queries executed."""
        result = await service.get_collection_memory_stats(
            user_id=user_id,
            contexts=[],
            is_workspace_owner=False,
        )

        assert result == {}
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_owner_sees_all_private_contexts(self, service, mock_db):
        """Workspace owner should see all private contexts (no user_id filter)."""
        owner_id = "owner_123"
        other_user_private = self._make_context(is_private=True, created_by="other_user")

        mock_row = MagicMock(context_id=other_user_private.id, memory_count=3)
        mock_result = MagicMock()
        mock_result.all.return_value = [mock_row]
        mock_db.execute.return_value = mock_result

        result = await service.get_collection_memory_stats(
            user_id=owner_id,
            contexts=[other_user_private],
            is_workspace_owner=True,
        )

        assert result[str(other_user_private.id)] == (3, 0)
