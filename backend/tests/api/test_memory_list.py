"""Tests for GET /memory/list endpoint (Issue #431).

Covers the new ``context_id`` query-param filter:
  - Omitted: legacy behavior, no PermissionService call.
  - Provided: PermissionService.resolve_context_for_workspace_read enforces
    access and ``Memory.context_id`` is added to both data and count queries.
  - Provided but context denied / not found: NotFoundException 404 propagates.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.routes.memory import list_memories
from utils.exceptions import NotFoundException

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
                q=None,
                tags=None,
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
                q=None,
                tags=None,
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

        with patch(
            "api.routes.memory.PermissionService", return_value=mock_perm_instance
        ) as mock_perm_cls:
            response = await list_memories(
                user=MOCK_USER,
                db=mock_db,
                scope=None,
                type=None,
                context_id=context_id,
                q=None,
                tags=None,
                limit=50,
                offset=0,
            )

        # Permission gate must still run on the shared path — without these
        # assertions a future refactor that accidentally drops the gate would
        # silently expose every shared-context memory to anyone who can guess
        # the context_id (CWE-639 regression).
        mock_perm_cls.assert_called_once_with(mock_db)
        mock_perm_instance.resolve_context_for_workspace_read.assert_awaited_once_with(
            user_id="test_user_123", context_id=context_id
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
                q=None,
                tags=None,
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
                q=None,
                tags=None,
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
        """PermissionService 404 on forbidden/missing context propagates as NotFoundException."""
        mock_db = AsyncMock()  # must not reach .execute()
        context_id = uuid4()

        mock_perm_instance = MagicMock()
        mock_perm_instance.resolve_context_for_workspace_read = AsyncMock(
            side_effect=NotFoundException("Context", str(context_id))
        )

        with patch("api.routes.memory.PermissionService", return_value=mock_perm_instance):
            with pytest.raises(NotFoundException) as exc:
                await list_memories(
                    user=MOCK_USER,
                    db=mock_db,
                    scope=None,
                    type=None,
                    context_id=context_id,
                    q=None,
                    limit=50,
                    offset=0,
                )

        assert exc.value.status_code == 404
        mock_db.execute.assert_not_called()


class TestListMemoriesQueryFilter:
    """Issue #580: optional ``q`` substring filter on GET /memory/list.

    The filter compiles to ``lower(memories.summary) LIKE lower(:param)`` under
    SQLAlchemy's default dialect — assertions target that exact substring so
    they aren't fooled by ``memories.summary`` appearing in ``select(Memory)``'s
    projection list (it does, by definition).
    """

    ILIKE_PREDICATE = "lower(memories.summary) LIKE lower("

    @pytest.mark.asyncio
    async def test_q_omitted_does_not_add_summary_filter(self):
        """When ``q`` is omitted, neither query carries the ILIKE predicate."""
        mem = _mock_memory_row()
        mock_db = _db_with_rows(total=1, rows=[mem])

        with patch("api.routes.memory.PermissionService"):
            response = await list_memories(
                user=MOCK_USER,
                db=mock_db,
                scope=None,
                type=None,
                context_id=None,
                q=None,
                tags=None,
                limit=50,
                offset=0,
            )

        assert response.total == 1
        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            assert self.ILIKE_PREDICATE not in sql, (
                f"summary ILIKE unexpectedly present when q omitted "
                f"(call_index={call_index}): {sql}"
            )

    @pytest.mark.asyncio
    async def test_q_provided_adds_summary_ilike_to_both_queries(self):
        """With ``q="hello"``, the ILIKE predicate appears in count + data queries."""
        mem = _mock_memory_row()
        mock_db = _db_with_rows(total=1, rows=[mem])

        with patch("api.routes.memory.PermissionService"):
            response = await list_memories(
                user=MOCK_USER,
                db=mock_db,
                scope=None,
                type=None,
                context_id=None,
                q="hello",
                tags=None,
                limit=50,
                offset=0,
            )

        assert response.total == 1
        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            assert self.ILIKE_PREDICATE in sql, (
                f"summary ILIKE missing when q provided (call_index={call_index}): {sql}"
            )

    @pytest.mark.asyncio
    async def test_q_whitespace_only_treated_as_none(self):
        """``q="   "`` normalizes to None — neither query carries the predicate."""
        mem = _mock_memory_row()
        mock_db = _db_with_rows(total=1, rows=[mem])

        with patch("api.routes.memory.PermissionService"):
            await list_memories(
                user=MOCK_USER,
                db=mock_db,
                scope=None,
                type=None,
                context_id=None,
                q="   ",
                tags=None,
                limit=50,
                offset=0,
            )

        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            assert self.ILIKE_PREDICATE not in sql, (
                f"summary ILIKE unexpectedly present for whitespace-only q "
                f"(call_index={call_index}): {sql}"
            )

    @pytest.mark.asyncio
    async def test_q_works_on_shared_context_without_user_id_filter(self):
        """Critical regression guard for #435: q filter must work for non-creator
        workspace members searching a shared context. The WHERE clause must
        contain the ILIKE predicate AND ``context_id`` AND must NOT carry a
        ``memories.user_id =`` predicate.
        """
        mem = _mock_memory_row()
        mock_db = _db_with_rows(total=1, rows=[mem])
        context_id = uuid4()

        mock_context = MagicMock()
        mock_context.is_private = False

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
                q="found",
                tags=None,
                limit=50,
                offset=0,
            )

        mock_perm_cls.assert_called_once_with(mock_db)
        mock_perm_instance.resolve_context_for_workspace_read.assert_awaited_once_with(
            user_id="test_user_123", context_id=context_id
        )
        assert response.total == 1

        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            assert self.ILIKE_PREDICATE in sql, (
                f"summary ILIKE missing on shared-context q path (call_index={call_index}): {sql}"
            )
            assert "context_id" in sql, (
                f"context_id predicate missing on shared-context q path "
                f"(call_index={call_index}): {sql}"
            )
            assert "memories.user_id" not in sql, (
                f"user_id predicate must be dropped for shared context "
                f"(call_index={call_index}): {sql}"
            )

    @pytest.mark.asyncio
    async def test_q_strips_surrounding_whitespace(self):
        """Surrounding whitespace is stripped before composing the ILIKE pattern."""
        mem = _mock_memory_row()
        mock_db = _db_with_rows(total=1, rows=[mem])

        with patch("api.routes.memory.PermissionService"):
            await list_memories(
                user=MOCK_USER,
                db=mock_db,
                scope=None,
                type=None,
                context_id=None,
                q="  hello  ",
                tags=None,
                limit=50,
                offset=0,
            )

        for call_index in (0, 1):
            stmt = mock_db.execute.call_args_list[call_index].args[0]
            literal_sql = str(stmt.whereclause.compile(compile_kwargs={"literal_binds": True}))
            assert "'%hello%'" in literal_sql, (
                f"trimmed ILIKE pattern missing (call_index={call_index}): {literal_sql}"
            )
            assert "'%  hello  %'" not in literal_sql, (
                f"un-trimmed ILIKE pattern leaked (call_index={call_index}): {literal_sql}"
            )

    @pytest.mark.asyncio
    async def test_q_escapes_like_wildcards(self):
        """SQL LIKE wildcards in user input must be escaped so ``q="50%"``
        searches for the literal substring "50%", not "50 followed by
        anything". Same for ``_`` (single-character wildcard) and ``\\``
        (the escape character itself)."""
        mem = _mock_memory_row()
        mock_db = _db_with_rows(total=1, rows=[mem])

        with patch("api.routes.memory.PermissionService"):
            await list_memories(
                user=MOCK_USER,
                db=mock_db,
                scope=None,
                type=None,
                context_id=None,
                q="50%_\\bar",
                tags=None,
                limit=50,
                offset=0,
            )

        for call_index in (0, 1):
            stmt = mock_db.execute.call_args_list[call_index].args[0]
            literal_sql = str(stmt.whereclause.compile(compile_kwargs={"literal_binds": True}))
            # The compiled pattern must contain the escaped form (each of %, _, \
            # prefixed with a backslash), surrounded by the implicit % wildcards.
            assert "'%50\\%\\_\\\\bar%'" in literal_sql, (
                f"escaped ILIKE pattern missing (call_index={call_index}): {literal_sql}"
            )
            # And the SQL must declare the escape character so the DB engine
            # interprets the backslash-prefixed metacharacters correctly.
            assert "ESCAPE '\\'" in literal_sql, (
                f"ESCAPE clause missing (call_index={call_index}): {literal_sql}"
            )


class TestListMemoriesTagsFilter:
    """Issue #618: ANY-match ``tags`` filter on GET /memory/list."""

    @pytest.mark.asyncio
    async def test_tags_filter_applied_to_both_queries(self):
        """When tags are given, the array-overlap predicate is added to the data
        AND count queries (so the total reflects the filter)."""
        mem = _mock_memory_row()
        mock_db = _db_with_rows(total=1, rows=[mem])

        response = await list_memories(
            user=MOCK_USER,
            db=mock_db,
            scope=None,
            type=None,
            context_id=None,
            q=None,
            tags=["python", "auth"],
            limit=50,
            offset=0,
        )

        assert response.total == 1
        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            # Array overlap (&&) on memories.tags — ANY-match.
            assert "tags" in sql and "&&" in sql, (
                f"tags overlap predicate missing (call_index={call_index}): {sql}"
            )

    @pytest.mark.asyncio
    async def test_no_tags_skips_filter(self):
        """Omitting tags leaves the tags predicate out of both queries."""
        mem = _mock_memory_row()
        mock_db = _db_with_rows(total=1, rows=[mem])

        await list_memories(
            user=MOCK_USER,
            db=mock_db,
            scope=None,
            type=None,
            context_id=None,
            q=None,
            tags=None,
            limit=50,
            offset=0,
        )

        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            assert "&&" not in sql, (
                f"tags overlap unexpectedly present when omitted (call_index={call_index}): {sql}"
            )

    @pytest.mark.asyncio
    async def test_blank_tags_ignored(self):
        """Whitespace-only / empty tag entries normalize to no filter, so a stray
        ``?tags=`` does not pin results to the empty set."""
        mem = _mock_memory_row()
        mock_db = _db_with_rows(total=1, rows=[mem])

        await list_memories(
            user=MOCK_USER,
            db=mock_db,
            scope=None,
            type=None,
            context_id=None,
            q=None,
            tags=["   ", ""],
            limit=50,
            offset=0,
        )

        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            assert "&&" not in sql, f"blank tags should be ignored (call_index={call_index}): {sql}"


class TestListMemoriesTagsMatch:
    """Issue #830: ``tags_match`` selects ANY (default) vs ALL tag matching."""

    @pytest.mark.asyncio
    async def test_tags_match_default_is_any_overlap(self):
        """Regression pin: omitting tags_match preserves #618 ANY-match (&&)."""
        mock_db = _db_with_rows(total=1, rows=[_mock_memory_row()])

        await list_memories(
            user=MOCK_USER,
            db=mock_db,
            scope=None,
            type=None,
            context_id=None,
            q=None,
            tags=["python", "auth"],
            limit=50,
            offset=0,
        )

        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            assert "&&" in sql and "@>" not in sql, (
                f"default tags_match must be ANY-overlap (&&), not @> "
                f"(call_index={call_index}): {sql}"
            )

    @pytest.mark.asyncio
    async def test_tags_match_all_uses_contains(self):
        """tags_match='all' switches to PG array contains (@>) on both queries."""
        mock_db = _db_with_rows(total=1, rows=[_mock_memory_row()])

        await list_memories(
            user=MOCK_USER,
            db=mock_db,
            scope=None,
            type=None,
            context_id=None,
            q=None,
            tags=["python", "auth"],
            tags_match="all",
            limit=50,
            offset=0,
        )

        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            assert "@>" in sql and "&&" not in sql, (
                f"tags_match='all' must use ALL-contains (@>), not && "
                f"(call_index={call_index}): {sql}"
            )

    @pytest.mark.asyncio
    async def test_tags_match_all_no_tags_skips_filter(self):
        """tags_match='all' without tags adds no predicate (no empty-set pin)."""
        mock_db = _db_with_rows(total=1, rows=[_mock_memory_row()])

        await list_memories(
            user=MOCK_USER,
            db=mock_db,
            scope=None,
            type=None,
            context_id=None,
            q=None,
            tags=None,
            tags_match="all",
            limit=50,
            offset=0,
        )

        for call_index in (0, 1):
            sql = _where_sql(mock_db, call_index)
            assert "@>" not in sql and "&&" not in sql, (
                f"no tags → no predicate (call_index={call_index}): {sql}"
            )
