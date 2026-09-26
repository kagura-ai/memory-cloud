"""CLIs refuse passwords longer than bcrypt's 72 bytes clearly (Issue #1707).

``hash_password`` raises ``PasswordTooLongError`` for > 72 bytes. The CLIs that
set a password check first, print the project message (never the password) and
either ask again before the confirmation prompt (interactive) or exit non-zero
before touching the database (``seed_e2e_admin``).
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from auth.password import PASSWORD_MAX_BYTES, PASSWORD_TOO_LONG_MESSAGE  # noqa: E402
from cli import create_admin, reset_password, seed_e2e_admin  # noqa: E402

LONG = "Aa1!" + "x" * 96  # passes the complexity rules, 100 bytes
VALID = "Valid-Pass-123!"
BANNER_LIMIT = f"Maximum {PASSWORD_MAX_BYTES} bytes"


class _StopAfterPasswordLoop(Exception):
    """Raised by the first prompt after the password loop."""


def _db_session(db: MagicMock) -> MagicMock:
    session = MagicMock()
    session.return_value.__enter__.return_value = db
    session.return_value.__exit__.return_value = False
    return session


class TestResetPassword:
    def test_long_password_is_refused_before_confirmation(self, capsys) -> None:
        user = SimpleNamespace(totp_enabled=False, totp_secret=None, password_hash="old")
        db = MagicMock()
        db.execute.return_value.scalar_one_or_none.return_value = user
        prompts: list[str] = []
        answers = iter([LONG, VALID, VALID])

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

        # The long password never reached the confirmation prompt nor hashing.
        assert prompts == ["New Password:", "New Password:", "Confirm:"]
        hp.assert_called_once_with(VALID)
        assert user.password_hash == "new-hash"
        out = capsys.readouterr().out
        assert PASSWORD_TOO_LONG_MESSAGE in out
        # The limit is stated in the requirements banner, before any refusal.
        assert BANNER_LIMIT in out
        assert out.index(BANNER_LIMIT) < out.index(PASSWORD_TOO_LONG_MESSAGE)
        assert LONG not in out


class TestCreateAdmin:
    def test_long_password_is_refused_before_confirmation(self, capsys) -> None:
        db = MagicMock()
        db.execute.return_value.scalar.return_value = 0
        prompts: list[str] = []
        answers = iter([LONG, VALID, VALID])

        def _getpass(prompt: str) -> str:
            prompts.append(prompt.strip())
            return next(answers)

        with (
            patch.object(create_admin, "create_engine"),
            patch.object(create_admin, "get_sync_database_url", return_value="x"),
            patch.object(create_admin, "Session", _db_session(db)),
            # login id, then the first prompt after the password loop stops the run
            patch("builtins.input", side_effect=["admin", _StopAfterPasswordLoop()]),
            patch.object(create_admin.getpass, "getpass", side_effect=_getpass),
        ):
            with pytest.raises(_StopAfterPasswordLoop):
                create_admin.create_admin(skip_mcp_json=True)

        assert prompts == ["Password:", "Password:", "Confirm:"]
        out = capsys.readouterr().out
        assert PASSWORD_TOO_LONG_MESSAGE in out
        # The limit is stated in the requirements banner, before any refusal.
        assert BANNER_LIMIT in out
        assert out.index(BANNER_LIMIT) < out.index(PASSWORD_TOO_LONG_MESSAGE)
        assert LONG not in out


class TestSeedE2eAdmin:
    def test_long_password_exits_non_zero_before_the_db(self, monkeypatch, capsys) -> None:
        monkeypatch.setenv("E2E_ADMIN_LOGIN_ID", "e2e-admin")
        monkeypatch.setenv("E2E_ADMIN_PASSWORD", LONG)

        with patch.object(seed_e2e_admin, "create_engine") as engine:
            with pytest.raises(SystemExit) as exc_info:
                seed_e2e_admin.seed_e2e_admin()

        assert exc_info.value.code == 1
        engine.assert_not_called()
        out = capsys.readouterr().out
        assert PASSWORD_TOO_LONG_MESSAGE in out
        assert LONG not in out
