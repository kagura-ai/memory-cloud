"""An operator's verified TOTP enrollment displays a usable backup set once."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from auth.recovery_codes import is_recovery_code, take_locked_code
from cli import create_admin, reset_password
from models.auth import User


@pytest.mark.parametrize("creating", [True, False], ids=["new-admin", "reenroll"])
def test_cli_enrollment_commits_hashes_and_displays_each_code_once(
    creating, monkeypatch, capsys, fernet_api_key_secret
):
    monkeypatch.setenv("API_KEY_SECRET", fernet_api_key_secret)
    module = create_admin if creating else reset_password
    db = MagicMock()
    session = MagicMock()
    session.return_value.__enter__.return_value = db
    monkeypatch.setattr(module, "create_engine", MagicMock())
    monkeypatch.setattr(module, "Session", session)
    monkeypatch.setattr(module, "get_sync_database_url", lambda: "unused")
    user = User(user_id="local:admin", email="admin@local", password_hash="old", totp_enabled=True)

    def verify_no_codes_displayed_before_commit():
        assert not any(is_recovery_code(line) for line in capsys.readouterr().out.splitlines())

    db.commit.side_effect = verify_no_codes_displayed_before_commit
    if creating:
        answers = iter(["admin", "y", "123456"])
        db.execute.return_value.scalar.return_value = 0
        monkeypatch.setattr(create_admin, "_leftover_owned_workspaces", lambda *a: 0)
        monkeypatch.setattr(
            create_admin, "_create_workspace", lambda *a: SimpleNamespace(id=None, name="test")
        )
        monkeypatch.setattr(create_admin, "_create_api_key", lambda *a: "test-key")
        monkeypatch.setattr(create_admin, "_configure_embedding_provider", lambda *a: None)
        monkeypatch.setattr(create_admin.getpass, "getpass", lambda *a: "Valid-Pass-123!")
        monkeypatch.setattr(create_admin, "verify_totp", lambda *a: True)
    else:
        answers = iter(["admin", "2", "y", "123456"])
        db.execute.return_value.scalar_one_or_none.return_value = user
        monkeypatch.setattr("auth.totp.verify_totp", lambda *a: True)

    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    if creating:
        create_admin.create_admin(skip_mcp_json=True)
        user = db.add.call_args.args[0]
    else:
        reset_password.reset_password()

    codes = [line for line in capsys.readouterr().out.splitlines() if is_recovery_code(line)]
    assert len(codes) == len(set(codes)) == 8
    assert all(take_locked_code(user, code) for code in codes)
    assert user.totp_recovery_hashes == []
