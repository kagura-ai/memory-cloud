"""``cli.delete_admin`` keeps the admin's connector keys (#2352).

In both branches the admin's live connector write keys are handed to their
workspace owners before the admin's other keys are deleted.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Delete, Update

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import delete_admin  # noqa: E402


def _run(total_users: int) -> list[Any]:
    statements: list[Any] = []
    admin = SimpleNamespace(user_id="local:admin", login_id="admin")

    def _execute(stmt, *a, **k):
        statements.append(stmt)
        result = MagicMock(rowcount=1)
        result.scalar_one_or_none.return_value = admin
        result.scalar.return_value = total_users
        return result

    db = MagicMock()
    db.execute.side_effect = _execute
    session = MagicMock()
    session.return_value.__enter__.return_value = db

    with (
        patch("cli.delete_admin.require_settings"),
        patch("cli.delete_admin.get_sync_database_url", return_value="postgresql://x/y"),
        patch("cli.delete_admin.create_engine"),
        patch("cli.delete_admin.Session", session),
        patch("builtins.input", return_value="y"),
    ):
        delete_admin.delete_admin()
    db.delete.assert_called_once_with(admin)
    db.commit.assert_called_once()
    return statements


@pytest.mark.parametrize("total_users", [2, 1])
def test_reown_runs_before_the_api_keys_delete(total_users: int, capsys):
    statements = _run(total_users)

    api_key_stmts = [s for s in statements if isinstance(s, Update | Delete)]
    reown, delete_keys = api_key_stmts[0], api_key_stmts[1]
    assert isinstance(reown, Update)
    sql = " ".join(str(reown.compile(dialect=postgresql.dialect())).split())
    assert sql.startswith(
        "UPDATE api_keys SET user_id=workspaces.owner_user_id FROM workspaces WHERE "
    )
    assert reown.compile().params["user_id_1"] == "local:admin"
    assert isinstance(delete_keys, Delete)
    assert getattr(delete_keys.table, "name", None) == "api_keys"
    assert "Connector write keys handed to their workspace owners: 1" in capsys.readouterr().out


def test_owned_workspaces_without_transfer_to_refuse_before_the_prompt(capsys):
    """#2371: the admin owns a workspace and other users exist — no prompt,
    no write, the workspaces are listed."""
    admin = SimpleNamespace(user_id="local:admin", login_id="admin")
    ws = SimpleNamespace(id="00000000-0000-0000-0000-0000000000aa", name="Default")
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = admin
    db.execute.return_value.scalar.return_value = 2
    session = MagicMock()
    session.return_value.__enter__.return_value = db

    with (
        patch("cli.delete_admin.require_settings"),
        patch("cli.delete_admin.get_sync_database_url", return_value="postgresql://x/y"),
        patch("cli.delete_admin.create_engine"),
        patch("cli.delete_admin.Session", session),
        patch("cli.delete_admin._owned_workspaces", return_value=[ws]),
        patch("builtins.input", side_effect=AssertionError("prompted")),
        pytest.raises(SystemExit) as exc_info,
    ):
        delete_admin.delete_admin()

    assert exc_info.value.code == 1
    db.delete.assert_not_called()
    db.commit.assert_not_called()
    out = capsys.readouterr().out
    assert "Default (00000000-0000-0000-0000-0000000000aa)" in out
    assert "--transfer-to" in out
