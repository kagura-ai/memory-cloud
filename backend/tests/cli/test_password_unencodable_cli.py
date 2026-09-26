"""CLIs refuse a password that cannot be UTF-8 encoded clearly (Issue #1718).

``os.environ`` decodes a non-UTF-8 byte as a lone surrogate
(``E2E_ADMIN_PASSWORD=$'Abc-123\\xff'`` reads as ``'Abc-123\\udcff'``), and
``getpass`` can return one when the password comes from a pipe. Such a password
used to end in a ``UnicodeEncodeError`` traceback. The CLIs print the project
message (never the password) the way they print the 72-byte one: the
interactive ones ask again before the confirmation prompt, ``seed_e2e_admin``
exits non-zero before touching the database.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from auth.password import PASSWORD_NOT_ENCODABLE_MESSAGE  # noqa: E402
from cli import create_admin, reset_password, seed_e2e_admin  # noqa: E402

UNENCODABLE = "Valid-Pass-123\udcff"  # passes the complexity rules
VALID = "Valid-Pass-123!"


class _StopAfterPasswordLoop(Exception):
    """Raised by the first prompt after the password loop."""


def _db_session(db: MagicMock) -> MagicMock:
    session = MagicMock()
    session.return_value.__enter__.return_value = db
    session.return_value.__exit__.return_value = False
    return session


def _assert_refused(out: str) -> None:
    assert PASSWORD_NOT_ENCODABLE_MESSAGE in out
    assert UNENCODABLE not in out


class TestResetPassword:
    def test_unencodable_password_is_refused_before_confirmation(self, capsys) -> None:
        user = SimpleNamespace(totp_enabled=False, totp_secret=None, password_hash="old")
        db = MagicMock()
        db.execute.return_value.scalar_one_or_none.return_value = user
        prompts: list[str] = []
        answers = iter([UNENCODABLE, VALID, VALID])

        def _getpass(prompt: str) -> str:
            prompts.append(prompt.strip())
            return next(answers)

        with (
            patch.object(reset_password, "create_engine"),
            patch.object(reset_password, "get_sync_database_url", return_value="x"),
            patch.object(reset_password, "Session", _db_session(db)),
            patch("builtins.input", side_effect=["admin", "1"]),
            patch.object(reset_password.getpass, "getpass", side_effect=_getpass),
            patch.object(reset_password, "hash_password", return_value="new-hash") as hp,
        ):
            reset_password.reset_password()

        assert prompts == ["New Password:", "New Password:", "Confirm:"]
        hp.assert_called_once_with(VALID)
        _assert_refused(capsys.readouterr().out)


class TestCreateAdmin:
    def test_unencodable_password_is_refused_before_confirmation(self, capsys) -> None:
        db = MagicMock()
        db.execute.return_value.scalar.return_value = 0
        prompts: list[str] = []
        answers = iter([UNENCODABLE, VALID, VALID])

        def _getpass(prompt: str) -> str:
            prompts.append(prompt.strip())
            return next(answers)

        with (
            patch.object(create_admin, "create_engine"),
            patch.object(create_admin, "get_sync_database_url", return_value="x"),
            patch.object(create_admin, "Session", _db_session(db)),
            patch("builtins.input", side_effect=["admin", _StopAfterPasswordLoop()]),
            patch.object(create_admin.getpass, "getpass", side_effect=_getpass),
        ):
            with pytest.raises(_StopAfterPasswordLoop):
                create_admin.create_admin(skip_mcp_json=True)

        assert prompts == ["Password:", "Password:", "Confirm:"]
        _assert_refused(capsys.readouterr().out)


class TestSeedE2eAdmin:
    def test_unencodable_password_exits_non_zero_before_the_db(self, monkeypatch, capsys) -> None:
        monkeypatch.setenv("E2E_ADMIN_LOGIN_ID", "e2e-admin")
        # os.environ stores it as the byte 0xff (surrogateescape) and reads it back.
        monkeypatch.setenv("E2E_ADMIN_PASSWORD", UNENCODABLE)

        with patch.object(seed_e2e_admin, "create_engine") as engine:
            with pytest.raises(SystemExit) as exc_info:
                seed_e2e_admin.seed_e2e_admin()

        assert exc_info.value.code == 1
        engine.assert_not_called()
        _assert_refused(capsys.readouterr().out)
