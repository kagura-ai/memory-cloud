"""The shared statements of a departing user's credentials (#2305, #2352, #2373).

Account erasure, member removal, the admin user delete and ``cli.delete_admin``
all run them before they delete a user's ``api_keys`` rows.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update

from services.connector_key_ownership import (
    SettledCredentials,
    count_agent_keys_stmt,
    lock_agents_stmt,
    reown_agents_stmt,
    reown_connector_keys,
    reown_connector_keys_stmt,
    settle_departing_credentials,
    suspend_agents_stmt,
)


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


# ---------------------------------------------------------------------------
# #2373: agents and agent-bound keys of the departing user
# ---------------------------------------------------------------------------


class TestAgentStatements:
    def test_the_lock_takes_both_agent_sets_in_id_order_without_workspaces(self):
        stmt = lock_agents_stmt("raw-sub")

        sql = _sql(stmt)
        assert sql.startswith("SELECT agents_1.id FROM agents AS agents_1 WHERE ")
        assert sql.endswith("ORDER BY agents_1.id FOR NO KEY UPDATE")
        # registered by the user, or losing its last live key with the user's
        assert "agents_1.owner_user_id = %(owner_user_id_1)s::VARCHAR OR " in sql
        assert "api_keys_2.agent_id = agents_1.id" in sql
        # a join would lock the workspace rows too
        assert "workspaces" not in sql

    def test_reown_names_the_owner_and_touches_nothing_else(self):
        ws = uuid4()
        stmt = reown_agents_stmt("raw-sub", workspace_id=ws)

        sql = _sql(stmt)
        assert sql.startswith(
            "UPDATE agents SET owner_user_id=workspaces.owner_user_id, updated_at=now() "
            "FROM workspaces WHERE agents.owner_user_id = %(owner_user_id_1)s::VARCHAR "
            "AND agents.workspace_id = %(workspace_id_1)s::UUID "
            "AND agents.workspace_id = workspaces.id "
            "AND workspaces.owner_user_id != %(owner_user_id_2)s::VARCHAR"
        )
        assert sql.endswith("RETURNING agents.workspace_id, workspaces.owner_user_id, agents.id")
        assert "status" not in sql
        assert stmt.compile().params == {
            "owner_user_id_1": "raw-sub",
            "workspace_id_1": ws,
            "owner_user_id_2": "raw-sub",
        }

    def test_suspend_takes_active_agents_whose_last_live_key_is_the_users(self):
        stmt = suspend_agents_stmt("raw-sub")

        sql = _sql(stmt)
        assert sql.startswith(
            "UPDATE agents SET status=%(status)s::VARCHAR, updated_at=now() FROM workspaces WHERE "
        )
        assert "agents.status = %(status_1)s::VARCHAR" in sql
        assert (
            "agents.id IN (SELECT api_keys_1.agent_id FROM api_keys AS api_keys_1 "
            "WHERE api_keys_1.user_id = %(user_id_1)s::VARCHAR "
            "AND api_keys_1.agent_id IS NOT NULL AND api_keys_1.revoked_at IS NULL)"
        ) in sql
        # another account's key keeps the agent only while it is usable:
        # neither revoked nor expired
        assert (
            "NOT (EXISTS (SELECT api_keys_2.id FROM api_keys AS api_keys_2 "
            "WHERE api_keys_2.agent_id = agents.id "
            "AND api_keys_2.user_id != %(user_id_2)s::VARCHAR "
            "AND api_keys_2.revoked_at IS NULL "
            "AND (api_keys_2.expires_at IS NULL "
            "OR api_keys_2.expires_at > %(expires_at_1)s::TIMESTAMP WITHOUT TIME ZONE)))"
        ) in sql
        assert "workspaces.owner_user_id != %(owner_user_id_1)s::VARCHAR" in sql
        assert "owner_user_id=" not in sql  # the owner stays
        assert sql.endswith("RETURNING agents.workspace_id, workspaces.owner_user_id, agents.id")
        params = stmt.compile().params
        assert params["status"] == "suspended"
        assert params["status_1"] == "active"

    def test_suspend_narrows_the_departing_keys_to_the_workspace(self):
        ws = uuid4()
        sql = _sql(suspend_agents_stmt("raw-sub", workspace_id=ws))

        assert "api_keys_1.workspace_id = %(workspace_id_1)s::UUID" in sql

    def test_the_agent_key_count_locks_the_rows_the_delete_takes(self):
        ws = uuid4()
        stmt = count_agent_keys_stmt("raw-sub", workspace_id=ws)

        sql = _sql(stmt)
        assert sql.startswith(
            "SELECT api_keys.workspace_id, workspaces.owner_user_id, count(*) AS count_1 "
        )
        assert (
            "api_keys.id IN (SELECT api_keys_1.id FROM api_keys AS api_keys_1 "
            "WHERE api_keys_1.user_id = %(user_id_1)s::VARCHAR "
            "AND api_keys_1.agent_id IS NOT NULL AND api_keys_1.revoked_at IS NULL "
            "AND api_keys_1.workspace_id = %(workspace_id_1)s::UUID "
            "ORDER BY api_keys_1.id FOR UPDATE)"
        ) in sql
        assert "workspaces.owner_user_id != %(owner_user_id_1)s::VARCHAR" in sql
        assert sql.endswith("GROUP BY api_keys.workspace_id, workspaces.owner_user_id")


def _results(*rows_per_statement: list[tuple]) -> tuple[MagicMock, list[Any]]:
    """A db whose ``execute`` answers each statement with the next rows."""
    captured: list[Any] = []
    answers = iter(rows_per_statement)

    def execute(stmt, *a, **k):
        captured.append(stmt)
        result = MagicMock()
        rows = next(answers, [])
        result.all.return_value = rows
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)
    db.flush = AsyncMock()
    return db, captured


class TestSettleDepartingCredentials:
    @pytest.mark.asyncio
    async def test_agents_first_then_keys_then_one_audit_row_per_owner_and_workspace(self):
        from config.settings import get_settings
        from models.auth import AuditLog
        from utils.hashing import hmac_sha256_hex

        ws_1, ws_2 = uuid4(), uuid4()
        low, mid, high = (UUID(int=n) for n in (1, 2, 3))
        db, captured = _results(
            [],  # the agent lock
            [(ws_1, "owner-1", mid)],  # agents re-owned
            # agents suspended, out of id order (RETURNING has none)
            [(ws_1, "owner-1", high), (ws_2, "owner-2", mid), (ws_1, "owner-1", low)],
            [(ws_1, "owner-1")],  # connector keys re-owned
            [(ws_1, "owner-1", 2), (ws_2, "owner-2", 1)],  # agent keys to delete
            [("owner-1", "one@example.com")],  # the owners' emails
        )

        settled = await settle_departing_credentials(db, "raw-sub", path="user_deletion")

        assert settled == SettledCredentials(
            connector_api_keys_reowned=1,
            agent_api_keys_deleted=3,
            agents_suspended=3,
            agents_reowned=1,
        )
        kinds = [_sql(stmt).split(" ", 2)[:2] for stmt in captured]
        assert kinds == [
            ["SELECT", "agents_1.id"],
            ["UPDATE", "agents"],
            ["UPDATE", "agents"],
            ["UPDATE", "api_keys"],
            ["SELECT", "api_keys.workspace_id,"],
            ["SELECT", "users.user_id,"],
        ]
        assert _sql(captured[3]).endswith("RETURNING api_keys.workspace_id, api_keys.user_id")

        (rows,) = db.add_all.call_args.args
        departing = hmac_sha256_hex("raw-sub", get_settings().audit_hmac_key)
        assert [
            (r.user_id, r.user_email, r.action, r.resource, r.new_value_hash, r.user_metadata)
            for r in rows
        ] == [
            (
                "owner-1",
                "one@example.com",
                "departed_member_credentials",
                "api_key",
                departing,
                {
                    "workspace_id": str(ws_1),
                    "path": "user_deletion",
                    "connector_api_keys_reowned": 1,
                    "agent_api_keys_deleted": 2,
                    "agents_suspended": 2,
                    "agents_reowned": 1,
                    # the agents to re-provision, in id order
                    "suspended_agent_ids": [str(low), str(high)],
                    "reowned_agent_ids": [str(mid)],
                },
            ),
            (
                "owner-2",
                "",  # no users row: the row is still written
                "departed_member_credentials",
                "api_key",
                departing,
                {
                    "workspace_id": str(ws_2),
                    "path": "user_deletion",
                    "connector_api_keys_reowned": 0,
                    "agent_api_keys_deleted": 1,
                    "agents_suspended": 1,
                    "agents_reowned": 0,
                    "suspended_agent_ids": [str(mid)],
                    "reowned_agent_ids": [],
                },
            ),
        ]
        assert all(isinstance(r, AuditLog) for r in rows)
        assert all("raw-sub" not in repr(r.user_metadata) for r in rows)
        db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_member_removal_narrows_every_statement_to_the_workspace(self):
        ws = uuid4()
        db, captured = _results()

        await settle_departing_credentials(db, "raw-sub", path="member_removal", workspace_id=ws)

        assert len(captured) == 5
        for stmt in captured:
            assert ws in stmt.compile().params.values(), _sql(stmt)

    @pytest.mark.asyncio
    async def test_nothing_settled_writes_no_audit_row(self):
        db, captured = _results()

        settled = await settle_departing_credentials(db, "raw-sub", path="erasure")

        assert settled == SettledCredentials()
        assert len(captured) == 5  # no owner lookup
        db.add_all.assert_not_called()
        db.flush.assert_not_awaited()
