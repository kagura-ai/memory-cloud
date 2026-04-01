"""Tests for async remember (Issue #76)."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.memory_service import MemoryService


class TestAsyncRemember:
    """Test that remember() returns immediately with pending status."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.commit = AsyncMock()
        db.flush = AsyncMock()
        db.rollback = AsyncMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def service(self, mock_db):
        return MemoryService(mock_db)

    @pytest.mark.asyncio
    async def test_remember_returns_immediately(self, service, mock_db):
        """remember() should commit to DB and return without waiting for embedding."""
        from models.schemas import RememberRequest

        request = RememberRequest(
            summary="Test async remember flow for issue 76",
            content="Testing that remember returns immediately",
            type="note",
            importance=0.5,
        )

        mock_context = MagicMock()
        mock_context.id = uuid4()
        mock_context.workspace_id = uuid4()

        service._get_context_isolation_params = AsyncMock(
            return_value=(mock_context, str(mock_context.workspace_id), str(mock_context.id))
        )
        service.memory_repo = MagicMock()
        service.memory_repo.create = AsyncMock()

        with (
            patch(
                "services.memory_service.process_pending_embedding",
                new=AsyncMock(),
            ),
            patch("services.quota_service.QuotaService"),
        ):
            result = await service.remember(
                request,
                user_id="test_user",
                client="test",
                current_context_id=mock_context.id,
                current_workspace_id=None,
            )

            assert result.scope == "working"
            assert result.memory_id is not None
            mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_remember_db_failure_rolls_back(self, service, mock_db):
        """If DB insert fails, should rollback and raise."""
        from models.schemas import RememberRequest

        request = RememberRequest(
            summary="Test DB failure during remember",
            content="Should rollback on error",
            type="note",
        )

        mock_context = MagicMock()
        mock_context.id = uuid4()
        mock_context.workspace_id = uuid4()

        service._get_context_isolation_params = AsyncMock(
            return_value=(mock_context, str(mock_context.workspace_id), str(mock_context.id))
        )
        service.memory_repo = MagicMock()
        service.memory_repo.create = AsyncMock(side_effect=Exception("DB error"))

        with pytest.raises(Exception, match="DB error"):
            await service.remember(
                request,
                user_id="test_user",
                client="test",
                current_context_id=mock_context.id,
                current_workspace_id=None,
            )

        mock_db.rollback.assert_called_once()

    @pytest.mark.asyncio
    async def test_remember_no_embedding_call(self, service, mock_db):
        """remember() should NOT call embedding service directly."""
        from models.schemas import RememberRequest

        request = RememberRequest(
            summary="Verify no embedding call in remember fast path",
            content="Embedding should happen in background only",
            type="note",
        )

        mock_context = MagicMock()
        mock_context.id = uuid4()
        mock_context.workspace_id = uuid4()

        service._get_context_isolation_params = AsyncMock(
            return_value=(mock_context, str(mock_context.workspace_id), str(mock_context.id))
        )
        service.memory_repo = MagicMock()
        service.memory_repo.create = AsyncMock()
        service.embedding_service.embed = AsyncMock()

        with (
            patch(
                "services.memory_service.process_pending_embedding",
                new=AsyncMock(),
            ),
            patch("services.quota_service.QuotaService"),
        ):
            await service.remember(
                request,
                user_id="test_user",
                client="test",
                current_context_id=mock_context.id,
                current_workspace_id=None,
            )

            service.embedding_service.embed.assert_not_called()
