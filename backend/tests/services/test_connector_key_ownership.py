"""The shared connector-key re-own statement (#2305, #2352).

Account erasure, member removal, the admin user delete and ``cli.delete_admin``
all run this UPDATE before they delete a user's ``api_keys`` rows.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update

from services.connector_key_ownership import reown_connector_keys, reown_connector_keys_stmt


def _sql(stmt: Any) -> str:
    return " ".join(str(stmt.compile(dialect=postgresql.dialect())).split())


class TestReownConnectorKeysStmt:
    def test_user_wide_statement(self):
        stmt = reown_connector_keys_stmt("raw-sub")

        assert isinstance(stmt, Update)
        sql = _sql(stmt)
        assert sql.startswith(
            "UPDATE api_keys SET user_id=workspaces.owner_user_id FROM workspaces WHERE "
        )
        # the id-ordered lock over the user's live connector rows
        assert (
            "api_keys.id IN (SELECT api_keys_1.id FROM api_keys AS api_keys_1 "
            "WHERE api_keys_1.user_id = %(user_id_1)s::VARCHAR "
            "AND api_keys_1.connector_id IS NOT NULL AND api_keys_1.revoked_at IS NULL "
            "ORDER BY api_keys_1.id FOR UPDATE)"
        ) in sql
        assert "api_keys.user_id = %(user_id_2)s::VARCHAR" in sql
        assert "api_keys.connector_id IS NOT NULL" in sql
        assert "api_keys.revoked_at IS NULL" in sql
        assert "api_keys.workspace_id = workspaces.id" in sql
        assert "workspaces.owner_user_id != %(owner_user_id_1)s::VARCHAR" in sql
        assert "workspace_id = %(workspace_id" not in sql
        assert stmt.compile().params == {
            "user_id_1": "raw-sub",
            "user_id_2": "raw-sub",
            "owner_user_id_1": "raw-sub",
        }

    def test_workspace_scope_narrows_the_lock_and_the_update(self):
        ws = uuid4()
        stmt = reown_connector_keys_stmt("raw-sub", workspace_id=ws)

        sql = _sql(stmt)
        assert (
            "api_keys.id IN (SELECT api_keys_1.id FROM api_keys AS api_keys_1 "
            "WHERE api_keys_1.user_id = %(user_id_1)s::VARCHAR "
            "AND api_keys_1.connector_id IS NOT NULL AND api_keys_1.revoked_at IS NULL "
            "AND api_keys_1.workspace_id = %(workspace_id_1)s::UUID "
            "ORDER BY api_keys_1.id FOR UPDATE)"
        ) in sql
        assert "api_keys.workspace_id = %(workspace_id_2)s::UUID" in sql
        assert "workspaces.owner_user_id != %(owner_user_id_1)s::VARCHAR" in sql
        assert stmt.compile().params == {
            "user_id_1": "raw-sub",
            "workspace_id_1": ws,
            "user_id_2": "raw-sub",
            "workspace_id_2": ws,
            "owner_user_id_1": "raw-sub",
        }


class TestReownConnectorKeys:
    @pytest.mark.asyncio
    async def test_returns_the_rowcount(self):
        captured: list[Any] = []
        db = MagicMock()
        db.execute = AsyncMock(
            side_effect=lambda stmt, *a, **k: captured.append(stmt) or MagicMock(rowcount=3)
        )
        ws = uuid4()

        assert await reown_connector_keys(db, "raw-sub", workspace_id=ws) == 3

        (stmt,) = captured
        assert isinstance(stmt, Update)
        assert stmt.compile().params["workspace_id_2"] == ws

    @pytest.mark.asyncio
    async def test_none_rowcount_is_zero(self):
        db = MagicMock()
        db.execute = AsyncMock(return_value=MagicMock(rowcount=None))

        assert await reown_connector_keys(db, "raw-sub") == 0
