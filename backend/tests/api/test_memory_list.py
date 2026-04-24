"""Tests for GET /memory/list endpoint (Issue #431).

Covers the new ``context_id`` query-param filter:
  - Omitted: legacy behavior, no PermissionService call.
  - Provided: PermissionService.resolve_context_for_workspace_read enforces
    access and ``Memory.context_id`` is added to both data and count queries.
  - Provided but context denied / not found: HTTPException 404 propagates.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.memory import list_memories

MOCK_USER = {"user_id": "test_user_123"}


def _mock_memory_row():
    mem = MagicMock()
    mem.id = uuid4()
    mem.summary = "Test memory"
    mem.type = "note"
    mem.scope = "persistent"
    mem.importance = 0.8
    mem.created_at = datetime(2026, 4, 1)
    mem.updated_at = datetime(2026, 4, 2)
    return mem


def _db_with_rows(total: int, rows: list):
    """Build an AsyncMock db whose .execute() returns count then row results."""
    mock_db = AsyncMock()

    mock_count_result = MagicMock()
    mock_count_result.scalar.return_value = total

    mock_rows_result = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = rows
    mock_rows_result.scalars.return_value = mock_scalars

    mock_db.execute.side_effect = [mock_count_result, mock_rows_result]
    return mock_db


class TestListMemoriesContextFilter:
    """Issue #431: context_id filter on GET /memory/list."""

    @pytest.mark.asyncio
    async def test_no_context_id_skips_permission_check(self):
        """Without context_id, legacy behavior holds — no PermissionService call."""
        mem = _mock_memory_row()
        mock_db = _db_with_rows(total=1, rows=[mem])

        with patch("api.routes.memory.PermissionService") as mock_perm_cls:
            response = await list_memories(
                user=MOCK_USER,
                db=mock_db,
                scope=None,
                type=None,
                context_id=None,
                limit=50,
                offset=0,
            )

        mock_perm_cls.assert_not_called()
        assert response.total == 1
        assert len(response.memories) == 1

    @pytest.mark.asyncio
    async def test_context_id_enforces_permission_and_filters(self):
        """With context_id, PermissionService is invoked and the filter is applied."""
        mem = _mock_memory_row()
        mock_db = _db_with_rows(total=1, rows=[mem])
        context_id = uuid4()

        mock_perm_instance = MagicMock()
        mock_perm_instance.resolve_context_for_workspace_read = AsyncMock()

        with patch(
            "api.routes.memory.PermissionService", return_value=mock_perm_instance
        ) as mock_perm_cls:
            response = await list_memories(
                user=MOCK_USER,
                db=mock_db,
                scope=None,
                type=None,
                context_id=context_id,
                limit=50,
                offset=0,
            )

        mock_perm_cls.assert_called_once_with(mock_db)
        mock_perm_instance.resolve_context_for_workspace_read.assert_awaited_once_with(
            user_id="test_user_123", context_id=context_id
        )
        assert response.total == 1

    @pytest.mark.asyncio
    async def test_context_id_denied_propagates_404(self):
        """PermissionService 404 on forbidden/missing context propagates as HTTPException."""
        mock_db = AsyncMock()  # must not reach .execute()
        context_id = uuid4()

        mock_perm_instance = MagicMock()
        mock_perm_instance.resolve_context_for_workspace_read = AsyncMock(
            side_effect=HTTPException(status_code=404, detail=f"Context {context_id} not found")
        )

        with patch("api.routes.memory.PermissionService", return_value=mock_perm_instance):
            with pytest.raises(HTTPException) as exc:
                await list_memories(
                    user=MOCK_USER,
                    db=mock_db,
                    scope=None,
                    type=None,
                    context_id=context_id,
                    limit=50,
                    offset=0,
                )

        assert exc.value.status_code == 404
        mock_db.execute.assert_not_called()
