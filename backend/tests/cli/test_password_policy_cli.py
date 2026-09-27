"""The admin CLIs validate through the shared password policy (Issue #1678).

``create_admin`` and ``reset_password`` ask ``validate_password_policy`` and
print its message, so the CLIs and the self-service endpoints can never drift
apart on a rule.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from auth.password_policy import PasswordPolicyError  # noqa: E402
from cli import create_admin, reset_password  # noqa: E402

REJECTED = "Rejected-Pass-123!"
VALID = "Valid-Pass-123!"
POLICY_MESSAGE = "policy-says-no"


class _StopAfterPasswordLoop(Exception):
    """Raised by the first prompt after the password loop."""


def _db_session(db: MagicMock) -> MagicMock:
    session = MagicMock()
    session.return_value.__enter__.return_value = db
    session.return_value.__exit__.return_value = False
    return session


def _policy(password: str) -> None:
    if password == REJECTED:
        raise PasswordPolicyError(POLICY_MESSAGE)


def _assert_policy_message(out: str) -> None:
    assert POLICY_MESSAGE in out
    assert REJECTED not in out


def test_reset_password_prints_the_policy_message(capsys) -> None:
    user = SimpleNamespace(
        user_id="local:admin", totp_enabled=False, totp_secret=None, password_hash="old"
    )
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = user
    prompts: list[str] = []
    answers = iter([REJECTED, VALID, VALID])

    def _getpass(prompt: str) -> str:
        prompts.append(prompt.strip())
        return next(answers)

    with (
        patch.object(reset_password, "create_engine"),
        patch.object(reset_password, "get_sync_database_url", return_value="x"),
        patch.object(reset_password, "Session", _db_session(db)),
        patch("builtins.input", side_effect=["admin", "1"]),
        patch.object(reset_password.getpass, "getpass", side_effect=_getpass),
        patch.object(reset_password, "hash_password", return_value="new-hash"),
        patch.object(reset_password, "validate_password_policy", side_effect=_policy) as policy,
    ):
        reset_password.reset_password()

    assert prompts == ["New Password:", "New Password:", "Confirm:"]
    assert [c.args[0] for c in policy.call_args_list] == [REJECTED, VALID]
    _assert_policy_message(capsys.readouterr().out)


def test_create_admin_prints_the_policy_message(capsys) -> None:
    db = MagicMock()
    db.execute.return_value.scalar.return_value = 0
    prompts: list[str] = []
    answers = iter([REJECTED, VALID, VALID])

    def _getpass(prompt: str) -> str:
        prompts.append(prompt.strip())
        return next(answers)

    with (
        patch.object(create_admin, "create_engine"),
        patch.object(create_admin, "get_sync_database_url", return_value="x"),
        patch.object(create_admin, "Session", _db_session(db)),
        patch("builtins.input", side_effect=["admin", _StopAfterPasswordLoop()]),
        patch.object(create_admin.getpass, "getpass", side_effect=_getpass),
        patch.object(create_admin, "validate_password_policy", side_effect=_policy) as policy,
    ):
        with pytest.raises(_StopAfterPasswordLoop):
            create_admin.create_admin(skip_mcp_json=True)

    assert prompts == ["Password:", "Password:", "Confirm:"]
    assert [c.args[0] for c in policy.call_args_list] == [REJECTED, VALID]
    _assert_policy_message(capsys.readouterr().out)


def test_reset_password_kills_outstanding_password_links() -> None:
    """A CLI reset invalidates emailed reset / set-up links in the same commit (#1678).

    Otherwise a link issued before the admin reset could overwrite the new
    password afterwards.
    """
    from sqlalchemy.sql.dml import Update

    user = SimpleNamespace(
        user_id="local:admin", totp_enabled=False, totp_secret=None, password_hash="old"
    )
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = user

    with (
        patch.object(reset_password, "create_engine"),
        patch.object(reset_password, "get_sync_database_url", return_value="x"),
        patch.object(reset_password, "Session", _db_session(db)),
        patch("builtins.input", side_effect=["admin", "1"]),
        patch.object(reset_password.getpass, "getpass", side_effect=[VALID, VALID]),
        patch.object(reset_password, "hash_password", return_value="new-hash"),
    ):
        reset_password.reset_password()

    names = [name for name, _args, _kwargs in db.mock_calls if name in ("execute", "commit")]
    updates = [
        (i, call.args[0])
        for i, call in enumerate(c for c in db.mock_calls if c[0] in ("execute", "commit"))
        if call[0] == "execute" and isinstance(call.args[0], Update)
    ]
    assert len(updates) == 1
    index, statement = updates[0]
    assert statement.table.name == "email_action_tokens"
    compiled = statement.compile()
    assert set(compiled.params["purpose_1"]) == {"reset_password", "set_password"}
    assert compiled.params["user_id_1"] == "local:admin"
    assert "commit" in names[index + 1 :]
    assert user.password_hash == "new-hash"
