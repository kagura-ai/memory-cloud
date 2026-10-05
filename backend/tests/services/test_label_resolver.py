"""Unit tests for ``services.label_resolver`` (#1861).

The shared batch resolvers behind the sleep-report and cost-aggregation
list rows: ``user_id → email`` (optionally narrowed to one workspace's
members) and ``workspace_id → name`` (live workspaces only). Both are
one query per call and make none for an empty id set.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from services.label_resolver import resolve_user_labels, resolve_workspace_names


def _db_returning(rows: list[tuple]) -> AsyncMock:
    result = MagicMock()
    result.all.return_value = rows
    return AsyncMock(execute=AsyncMock(return_value=result))


def _compiled_sql(db: AsyncMock) -> str:
    stmt = db.execute.await_args.args[0]
    return str(stmt.compile(dialect=postgresql.dialect()))


class TestResolveUserLabels:
    @pytest.mark.asyncio
    async def test_maps_ids_to_emails_and_omits_unresolved(self):
        db = _db_returning([("human", "h@test.com")])  # 'connector_x' absent
        labels = await resolve_user_labels(db, {"human", "connector_x"})
        assert labels == {"human": "h@test.com"}
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_input_skips_query(self):
        db = AsyncMock()
        assert await resolve_user_labels(db, set()) == {}
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_admin_lookup_is_not_narrowed(self):
        db = _db_returning([])
        await resolve_user_labels(db, {"u1"})
        assert "workspace_members" not in _compiled_sql(db)

    @pytest.mark.asyncio
    async def test_member_of_narrows_to_current_workspace_members(self):
        """A workspace owner sees the emails of current members only: an
        account that wrote in the workspace and left, or that only wrote
        through a connector, stays a shortened id."""
        db = _db_returning([])
        workspace_id = uuid4()
        await resolve_user_labels(db, {"u1"}, member_of=workspace_id)
        sql = _compiled_sql(db)
        assert "workspace_members" in sql
        assert "workspace_members.workspace_id" in sql
        assert "users.user_id IN (SELECT workspace_members.user_id" in sql


class TestResolveWorkspaceNames:
    @pytest.mark.asyncio
    async def test_maps_ids_to_names(self):
        wid = uuid4()
        db = _db_returning([(wid, "Team Alpha")])
        assert await resolve_workspace_names(db, {wid, uuid4()}) == {wid: "Team Alpha"}
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_input_skips_query(self):
        db = AsyncMock()
        assert await resolve_workspace_names(db, set()) == {}
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_soft_deleted_workspaces_are_excluded(self):
        """A deleted workspace's rows fall back to the id rather than wearing
        a live-looking name."""
        db = _db_returning([])
        await resolve_workspace_names(db, {uuid4()})
        assert "workspaces.deleted_at IS NULL" in _compiled_sql(db)
