"""``cli.delete_admin`` (#2352, #2371), without a database.

The admin's live connector write keys are handed to their workspace owners
before its other keys are deleted; with other users present, a workspace the
admin owns that they are in stops the command unless ``--transfer-to`` names
who takes it, and a change while the prompt waits stops the delete. The
database paths are in ``tests/integration/test_delete_admin_cli_db.py``.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.sql.dml import Delete

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import delete_admin  # noqa: E402

_ADMIN = SimpleNamespace(user_id="local:admin", login_id="admin")


def _db(total_users: int) -> tuple[MagicMock, list[Any]]:
    calls: list[Any] = []

    async def _execute(stmt, *a, **k):
        calls.append(stmt)
        result = MagicMock(rowcount=1)
        result.scalar.return_value = total_users
        result.scalars.return_value.all.return_value = ["ws-1"]
        result.scalar_one_or_none.return_value = _ADMIN
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.delete = AsyncMock(side_effect=lambda row: calls.append(("delete", row)))
    db.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
    db.rollback = AsyncMock(side_effect=lambda: calls.append("rollback"))
    return db, calls


def _members(**by_ws: set[str]) -> AsyncMock:
    return AsyncMock(return_value=defaultdict(set, by_ws))


async def _run(db: MagicMock, owned, members=None, ask=lambda prompt: True) -> int:
    async def _reown(session, user_id):
        await session.execute("reown")
        return 1

    with (
        patch.object(delete_admin, "_password_admin", AsyncMock(return_value=_ADMIN)),
        patch.object(delete_admin, "_owned_workspaces", owned),
        patch.object(delete_admin, "_members", members or _members()),
        patch.object(delete_admin, "reown_connector_keys", side_effect=_reown),
    ):
        return await delete_admin.delete_password_admin(db, transfer_to=None, ask=ask)


def _writes(calls: list[Any]) -> list[Any]:
    return [
        getattr(c.table, "name", None) if isinstance(c, Delete) else c
        for c in calls
        if isinstance(c, Delete) or c in ("reown", "commit")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("total_users", "tables"),
    [
        (2, ["reown", "api_keys", "workspace_members"]),
        (1, ["reown", "api_keys", "workspace_members", "workspaces"]),
    ],
)
async def test_reown_runs_before_the_deletes(total_users: int, tables: list[str], capsys):
    db, calls = _db(total_users)

    assert await _run(db, AsyncMock(return_value=[])) == 0

    assert _writes(calls) == [*tables, "commit"]
    assert calls[-2:] == [("delete", _ADMIN), "commit"]
    assert "Connector write keys handed to their workspace owners: 1" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_shared_workspace_without_transfer_to_refuses_before_the_prompt(capsys):
    db, calls = _db(2)
    ws = SimpleNamespace(id="00000000-0000-0000-0000-0000000000aa", name="Default")

    code = await _run(
        db,
        AsyncMock(return_value=[ws]),
        members=_members(**{ws.id: {_ADMIN.user_id, "bob"}}),
        ask=MagicMock(side_effect=AssertionError("prompted")),
    )

    assert code == 1
    assert _writes(calls) == []
    out = capsys.readouterr().out
    assert "Default (00000000-0000-0000-0000-0000000000aa)" in out
    assert "--transfer-to" in out


@pytest.mark.asyncio
async def test_a_workspace_created_while_the_prompt_waits_stops_the_delete(capsys):
    db, calls = _db(2)
    new = SimpleNamespace(id="00000000-0000-0000-0000-0000000000bb", name="New")
    # live owned before the prompt, soft-deleted owned, live owned after it
    owned = AsyncMock(side_effect=[[], [], [new]])

    assert await _run(db, owned) == 1

    assert _writes(calls) == []
    assert "changed while the prompt waited" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_main_reports_a_failure_as_an_error_line(capsys):
    async def _get_db():
        yield MagicMock()

    with (
        patch.object(delete_admin, "require_settings"),
        patch.object(delete_admin, "get_db", _get_db),
        patch.object(
            delete_admin, "delete_password_admin", AsyncMock(side_effect=RuntimeError("boom"))
        ),
    ):
        code = await delete_admin._main(delete_admin._parse([]))

    assert code == 1
    assert "error: boom" in capsys.readouterr().err
