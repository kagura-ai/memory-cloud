"""``cli.delete_admin`` (#2352, #2371), without a database.

The admin's live connector write keys are handed to their workspace owners
before its other keys are deleted; with other users present, workspaces the
admin owns stop the command unless ``--transfer-to`` names who takes them.
The database paths are in ``tests/integration/test_delete_admin_cli_db.py``.
"""

from __future__ import annotations

import sys
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
        return result

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.delete = AsyncMock(side_effect=lambda row: calls.append(("delete", row)))
    db.commit = AsyncMock(side_effect=lambda: calls.append("commit"))
    return db, calls


async def _run(db: MagicMock, owned: list, transfer_to: str | None = None) -> int:
    async def _reown(session, user_id):
        await session.execute("reown")
        return 1

    with (
        patch.object(delete_admin, "_password_admin", AsyncMock(return_value=_ADMIN)),
        patch.object(delete_admin, "_owned_workspaces", AsyncMock(return_value=owned)),
        patch.object(delete_admin, "reown_connector_keys", side_effect=_reown),
    ):
        return await delete_admin.delete_password_admin(
            db, transfer_to=transfer_to, ask=lambda prompt: True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("total_users", [2, 1])
async def test_reown_runs_before_the_deletes(total_users: int, capsys):
    db, calls = _db(total_users)

    assert await _run(db, owned=[]) == 0

    tables = [
        getattr(c.table, "name", None) if isinstance(c, Delete) else c
        for c in calls
        if isinstance(c, Delete) or c == "reown"
    ]
    assert tables == ["reown", "api_keys", "workspace_members", "workspaces"]
    assert calls[-2:] == [("delete", _ADMIN), "commit"]
    assert "Connector write keys handed to their workspace owners: 1" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_owned_workspaces_without_transfer_to_refuse_before_the_prompt(capsys):
    db, calls = _db(2)
    ws = SimpleNamespace(id="00000000-0000-0000-0000-0000000000aa", name="Default")

    with patch.object(delete_admin, "_password_admin", AsyncMock(return_value=_ADMIN)):
        with patch.object(delete_admin, "_owned_workspaces", AsyncMock(return_value=[ws])):
            code = await delete_admin.delete_password_admin(
                db, transfer_to=None, ask=MagicMock(side_effect=AssertionError("prompted"))
            )

    assert code == 1
    assert not [c for c in calls if isinstance(c, Delete) or c == "commit"]
    out = capsys.readouterr().out
    assert "Default (00000000-0000-0000-0000-0000000000aa)" in out
    assert "--transfer-to" in out


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
