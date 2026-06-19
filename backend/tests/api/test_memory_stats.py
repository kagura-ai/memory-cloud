"""Tests for memory usage stats endpoint (Issue #83)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.contexts import get_memory_usage_stats


def _mock_perm_service():
    """Create a mock PermissionService that passes all checks."""
    mock_cls = MagicMock()
    mock_instance = MagicMock()
    mock_instance.check_context_access = AsyncMock()
    mock_cls.return_value = mock_instance
    return mock_cls


MOCK_USER = {"user_id": "test_user_123"}


class TestGetMemoryUsageStats:
    """Test GET /contexts/{context_id}/memory-stats endpoint."""

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def context_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_returns_paginated_stats(self, mock_db, context_id):
        """Test that endpoint returns paginated memory stats."""
        mem = MagicMock()
        mem.id = uuid4()
        mem.summary = "Test memory"
        mem.type = "note"
        mem.importance = 0.8
        mem.scope = "persistent"
        mem.access_count = 10
        mem.reference_count = 5
        mem.last_used_at = datetime(2026, 4, 1)
        mem.embedding_status = "success"
        mem.created_at = datetime(2026, 3, 1)

        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 1

        mock_mem_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mem]
        mock_mem_result.scalars.return_value = mock_scalars

        mock_db.execute.side_effect = [mock_count_result, mock_mem_result]

        with patch("services.permission_service.PermissionService", _mock_perm_service()):
            response = await get_memory_usage_stats(
                context_id=context_id,
                sort_by="reference_count",
                sort_order="desc",
                limit=50,
                offset=0,
                user=MOCK_USER,
                db=mock_db,
            )

        assert response.total == 1
        assert len(response.memories) == 1
        # Issue #1046: adoption signal surfaced; dead always-zero use_count removed.
        assert response.memories[0].reference_count == 5
        assert response.memories[0].access_count == 10
        assert response.memories[0].importance == 0.8
        assert response.sort_by == "reference_count"

    @pytest.mark.asyncio
    async def test_invalid_sort_field_raises_400(self, mock_db, context_id):
        """Test that invalid sort_by field returns 400."""
        with patch("services.permission_service.PermissionService", _mock_perm_service()):
            with pytest.raises(HTTPException) as exc:
                await get_memory_usage_stats(
                    context_id=context_id,
                    sort_by="invalid_field",
                    sort_order="desc",
                    limit=50,
                    offset=0,
                    user=MOCK_USER,
                    db=mock_db,
                )

        assert exc.value.status_code == 400
        assert "Invalid sort_by" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_empty_context_returns_empty(self, mock_db, context_id):
        """Test that empty context returns zero results."""
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0

        mock_mem_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_mem_result.scalars.return_value = mock_scalars

        mock_db.execute.side_effect = [mock_count_result, mock_mem_result]

        with patch("services.permission_service.PermissionService", _mock_perm_service()):
            response = await get_memory_usage_stats(
                context_id=context_id,
                sort_by="access_count",
                sort_order="desc",
                limit=50,
                offset=0,
                user=MOCK_USER,
                db=mock_db,
            )

        assert response.total == 0
        assert response.memories == []

    @pytest.mark.asyncio
    async def test_dead_use_count_sort_now_rejected(self, mock_db, context_id):
        """Issue #1046: the dead ``use_count`` sort field was removed → 400."""
        with patch("services.permission_service.PermissionService", _mock_perm_service()):
            with pytest.raises(HTTPException) as exc:
                await get_memory_usage_stats(
                    context_id=context_id,
                    sort_by="use_count",
                    sort_order="desc",
                    limit=50,
                    offset=0,
                    user=MOCK_USER,
                    db=mock_db,
                )

        assert exc.value.status_code == 400
        assert "Invalid sort_by" in str(exc.value.detail)
