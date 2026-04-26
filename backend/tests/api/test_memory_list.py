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


def _where_sql(mock_db, call_index: int) -> str:
    """Return the compiled WHERE clause for the Select passed to mock_db.execute(...).

    We target just the WHERE subtree (not the full SELECT) because the column
    list of ``select(Memory)`` always expands to include ``memories.context_id``
    as a projected column — a whole-statement string match would be useless.
    """
    stmt = mock_db.execute.call_args_list[call_index].args[0]
    whereclause = stmt.whereclause
    return str(whereclause.compile(compile_kwargs={"literal_binds": False}))


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

        # Regression guard: when context_id is omitted, the predicate must
        # NOT be present in either the count query or the data query.
        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            assert "context_id" not in sql, (
                f"context_id unexpectedly appears in SQL when param omitted: {sql}"
            )

    @pytest.mark.asyncio
    async def test_context_id_enforces_permission_and_filters_private(self):
        """Private context: caller's user_id stays in the WHERE clause —
        only the creator sees their own memories.
        """
        mem = _mock_memory_row()
        mock_db = _db_with_rows(total=1, rows=[mem])
        context_id = uuid4()

        mock_context = MagicMock()
        mock_context.is_private = True

        mock_perm_instance = MagicMock()
        mock_perm_instance.resolve_context_for_workspace_read = AsyncMock(return_value=mock_context)

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

        # Regression guard: BOTH the count query and the data query must
        # carry the context_id predicate AND the user_id predicate (private
        # context = creator-only).
        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            assert "context_id" in sql, (
                f"context_id predicate missing from SQL (call_index={call_index}): {sql}"
            )
            assert "user_id" in sql, (
                f"user_id predicate missing for private context (call_index={call_index}): {sql}"
            )

    @pytest.mark.asyncio
    async def test_context_id_shared_drops_user_id_filter(self):
        """Shared context: user_id filter is dropped so workspace members
        see every memory in the context, not only their own.

        Mirrors the graph endpoint's
        ``owner_filter = user_id if context.is_private else None`` pattern
        — without this, admin/non-creator viewers see an empty list even
        though PermissionService granted access.
        """
        mem = _mock_memory_row()
        mock_db = _db_with_rows(total=1, rows=[mem])
        context_id = uuid4()

        mock_context = MagicMock()
        mock_context.is_private = False

        mock_perm_instance = MagicMock()
        mock_perm_instance.resolve_context_for_workspace_read = AsyncMock(return_value=mock_context)

        with patch("api.routes.memory.PermissionService", return_value=mock_perm_instance):
            response = await list_memories(
                user=MOCK_USER,
                db=mock_db,
                scope=None,
                type=None,
                context_id=context_id,
                limit=50,
                offset=0,
            )

        assert response.total == 1

        # Regression guard: with a shared context, the WHERE clause must
        # NOT contain a `memories.user_id =` predicate in either query.
        # `context_id` MUST still be present so other contexts don't leak.
        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            assert "context_id" in sql, (
                f"context_id predicate missing from SQL (call_index={call_index}): {sql}"
            )
            assert "user_id" not in sql, (
                f"user_id predicate must be dropped for shared context "
                f"(call_index={call_index}): {sql}"
            )

    @pytest.mark.asyncio
    async def test_null_updated_at_falls_back_to_created_at(self):
        """Memory.updated_at is nullable (set onupdate). Fresh-insert rows have
        updated_at=NULL; serializer must not crash and should fall back to
        created_at to keep the response shape stable."""
        mem = _mock_memory_row()
        mem.updated_at = None  # simulates a never-updated row
        mock_db = _db_with_rows(total=1, rows=[mem])

        with patch("api.routes.memory.PermissionService"):
            response = await list_memories(
                user=MOCK_USER,
                db=mock_db,
                scope=None,
                type=None,
                context_id=None,
                limit=50,
                offset=0,
            )

        assert response.total == 1
        assert response.memories[0].updated_at == response.memories[0].created_at

    @pytest.mark.asyncio
    async def test_soft_deleted_memories_excluded(self):
        """``deleted_at IS NULL`` is in WHERE on every code path (regression #433):
        forget() is a soft-delete, so the list must hide tombstones."""
        mock_db = _db_with_rows(total=0, rows=[])

        with patch("api.routes.memory.PermissionService"):
            await list_memories(
                user=MOCK_USER,
                db=mock_db,
                scope=None,
                type=None,
                context_id=None,
                limit=50,
                offset=0,
            )

        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            assert "deleted_at IS NULL" in sql, (
                f"deleted_at filter missing (call_index={call_index}): {sql}"
            )

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
