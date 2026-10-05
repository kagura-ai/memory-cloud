"""A CLI password reset signs the account out everywhere (Issue #1866).

``reset_password`` is the operator's compromise-recovery path, so choices 1
and 3 must do what the emailed-link reset does, in one transaction:

- lock the owner and revoke the OAuth / MCP grants in the documented order
  ``users -> oauth_authorization_codes -> oauth_device_codes -> oauth_tokens``;
- forget the known devices;
- delete every browser session with ``strict=True`` BEFORE the commit;
- change nothing when the session store cannot be reached or the delete
  fails, and never report success then.

Choice 2 (disable MFA only) rotates no credential and revokes nothing.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

_BACKEND_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from cli import reset_password  # noqa: E402

VALID = "Valid-Pass-123!"
USER_ID = "local:admin"
_ROWCOUNTS = {
    "oauth_authorization_codes": 2,
    "oauth_device_codes": 1,
    "oauth_tokens": 4,
}


class _StoreDown(Exception):
    """The session store refused the connection or the delete."""


def _user() -> SimpleNamespace:
    return SimpleNamespace(
        user_id=USER_ID, totp_enabled=True, totp_secret="enc", password_hash="old-hash"
    )


def _table_of(statement: Any) -> str:
    table = getattr(statement, "table", None)
    return table.name if table is not None else statement.get_final_froms()[0].name


class _Run:
    """One CLI run against a mocked database and session store."""

    def __init__(
        self,
        choice: str,
        *,
        store_error: Exception | None = None,
        delete_error: Exception | None = None,
        commit_error: Exception | None = None,
        redis_url: str = "redis://store:6379",
    ) -> None:
        self.choice = choice
        self.user = _user()
        self.events: list[Any] = []
        self.db = MagicMock()
        self.db.execute.side_effect = self._execute
        self.db.commit.side_effect = self._commit
        self.commit_error = commit_error
        self.redis_url = redis_url
        self.db.rollback.side_effect = lambda: self.events.append("rollback")
        self.manager = MagicMock()
        self.manager.delete_user_sessions.side_effect = self._delete_sessions
        self.manager_cls = MagicMock(return_value=self.manager)
        if store_error is not None:
            self.manager_cls.side_effect = store_error
        self.delete_error = delete_error
        self.getpass = MagicMock(side_effect=[VALID, VALID])

    def _execute(self, statement: Any) -> MagicMock:
        self.events.append(statement)
        result = MagicMock()
        result.scalar_one_or_none.return_value = self.user
        result.rowcount = _ROWCOUNTS.get(_table_of(statement), 0)
        return result

    def _commit(self) -> None:
        if self.commit_error is not None:
            self.events.append("commit-failed")
            raise self.commit_error
        self.events.append("commit")

    def _delete_sessions(self, *args: Any, **kwargs: Any) -> int:
        self.events.append(("sessions", args, kwargs))
        if self.delete_error is not None:
            raise self.delete_error
        return 3

    def run(self) -> None:
        session = MagicMock()
        session.return_value.__enter__.return_value = self.db
        session.return_value.__exit__.return_value = False
        # Choices 2 and 3 end with the "Re-enable MFA now?" prompt.
        answers = ["admin", self.choice] + (["n"] if self.choice in ("2", "3") else [])
        with (
            patch.object(reset_password, "create_engine"),
            patch.object(reset_password, "get_sync_database_url", return_value="x"),
            patch.object(reset_password, "get_redis_url", return_value=self.redis_url),
            patch.object(reset_password, "Session", session),
            patch.object(reset_password, "SessionManager", self.manager_cls),
            patch("builtins.input", side_effect=answers),
            patch.object(reset_password.getpass, "getpass", self.getpass),
            patch.object(reset_password, "hash_password", return_value="new-hash"),
        ):
            reset_password.reset_password()

    @property
    def statements(self) -> list[Any]:
        """The statements executed after the user lookup."""
        return [e for e in self.events if not isinstance(e, str | tuple)][1:]

    def index_of(self, event: Any) -> int:
        return self.events.index(event)


@pytest.mark.parametrize("choice", ["1", "3"])
def test_password_reset_revokes_grants_in_the_documented_lock_order(choice: str) -> None:
    run = _Run(choice)
    run.run()

    lock, codes, devices, tokens = run.statements[:4]
    assert type(lock).__name__ == "Select"
    assert lock.get_final_froms()[0].name == "users"
    assert lock._for_update_arg is not None and not lock._for_update_arg.read
    assert lock.compile().params == {"user_id_1": USER_ID}
    assert [type(s).__name__ for s in (codes, devices, tokens)] == ["Delete", "Delete", "Update"]
    assert [s.table.name for s in (codes, devices, tokens)] == [
        "oauth_authorization_codes",
        "oauth_device_codes",
        "oauth_tokens",
    ]
    for statement in (codes, devices, tokens):
        assert statement.compile().params["user_id_1"] == USER_ID


@pytest.mark.parametrize("choice", ["1", "3"])
def test_password_reset_forgets_known_devices_and_password_links(choice: str) -> None:
    run = _Run(choice)
    run.run()

    by_table = {_table_of(s): s for s in run.statements}
    devices = by_table["user_known_devices"]
    assert type(devices).__name__ == "Delete"
    assert devices.compile().params == {"user_id_1": USER_ID}
    assert type(by_table["email_action_tokens"]).__name__ == "Update"
    commit = run.index_of("commit")
    assert all(run.index_of(s) < commit for s in run.statements)


@pytest.mark.parametrize("choice", ["1", "3"])
def test_password_reset_deletes_sessions_strictly_before_the_commit(choice: str) -> None:
    run = _Run(choice)
    run.run()

    run.manager_cls.assert_called_once_with(redis_url="redis://store:6379")
    run.manager.delete_user_sessions.assert_called_once_with(USER_ID, strict=True)
    sessions = next(e for e in run.events if isinstance(e, tuple))
    # Everything the transaction writes is sent first, then the sessions go,
    # then the one commit: the hash and the revocations land together.
    assert all(run.index_of(s) < run.index_of(sessions) for s in run.statements)
    assert run.index_of(sessions) < run.index_of("commit")
    assert run.events.count("commit") == 1
    assert "rollback" not in run.events
    assert run.user.password_hash == "new-hash"
    assert run.user.totp_enabled is (choice == "1")


def test_password_reset_reports_what_was_revoked_and_what_was_not(capsys) -> None:
    run = _Run("1")
    run.run()

    out = capsys.readouterr().out
    assert "Password updated" in out
    assert "3 browser session(s)" in out
    assert "4 OAuth / MCP token(s)" in out
    assert "3 pending authorization / device code(s)" in out
    assert "API keys and OAuth client secrets" in out
    assert "NOT revoked" in out


@pytest.mark.parametrize("choice", ["1", "3"])
@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://store:6379",
        "redis://:s3cret@store:6379/0",
        "redis://user:s3cret@store:6379/0",
        "redis://store:6379/0?password=s3cret",
    ],
)
def test_unreachable_session_store_aborts_before_anything_changes(
    choice: str, redis_url: str, capsys
) -> None:
    run = _Run(
        choice,
        store_error=ConnectionError(f"Failed to connect to Redis at {redis_url}"),
        redis_url=redis_url,
    )

    with pytest.raises(SystemExit) as exit_info:
        run.run()

    assert exit_info.value.code == 1
    # Refused before the operator types a password, and nothing was written.
    run.getpass.assert_not_called()
    assert run.statements == []
    assert "commit" not in run.events
    assert run.user.password_hash == "old-hash"
    assert run.user.totp_enabled is True
    captured = capsys.readouterr()
    out = captured.out
    assert "Password updated" not in out
    assert "Done for" not in out
    assert "Cannot reach the session store at store:6379 " in out
    assert "Nothing was changed" in out
    # Only host and port are printed: no userinfo, no query, no exception text.
    assert "s3cret" not in out + captured.err
    assert "user" not in out


def test_unusable_redis_url_is_refused_without_echoing_it(capsys) -> None:
    run = _Run("1", store_error=ConnectionError("x"), redis_url="redis://:s3cret@store:notaport")

    with pytest.raises(SystemExit) as exit_info:
        run.run()

    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert "s3cret" not in out
    assert "Nothing was changed" in out


def test_missing_redis_package_is_not_reported_as_an_unreachable_store(capsys) -> None:
    run = _Run("1", store_error=ImportError("redis package not installed"))

    with pytest.raises(SystemExit) as exit_info:
        run.run()

    assert exit_info.value.code == 1
    run.getpass.assert_not_called()
    assert run.statements == []
    out = capsys.readouterr().out
    assert "Cannot reach" not in out
    assert "'redis' package" in out
    assert "Nothing was changed" in out


def test_any_other_store_failure_still_aborts_before_anything_changes(capsys) -> None:
    run = _Run("1", store_error=RuntimeError("s3cret"))

    with pytest.raises(SystemExit) as exit_info:
        run.run()

    assert exit_info.value.code == 1
    run.getpass.assert_not_called()
    assert run.statements == []
    out = capsys.readouterr().out
    assert "RuntimeError" in out and "s3cret" not in out
    assert "Nothing was changed" in out


@pytest.mark.parametrize("choice", ["1", "3"])
def test_failed_commit_after_the_session_delete_is_reported_without_details(
    choice: str, capsys
) -> None:
    # A driver error carries the statement and its parameters — the new hash.
    run = _Run(choice, commit_error=_StoreDown("UPDATE users SET password_hash='new-hash'"))

    with pytest.raises(SystemExit) as exit_info:
        run.run()

    assert exit_info.value.code == 1
    assert run.events[-2:] == ["commit-failed", "rollback"]
    assert "commit" not in run.events
    captured = capsys.readouterr()
    out = captured.out
    assert "new-hash" not in out + captured.err
    assert "_StoreDown" in out
    assert "Password updated" not in out
    assert "MFA disabled" not in out
    assert "Done for" not in out
    assert "password was NOT changed" in out
    assert "already signed out" in out
    assert "again" in out


def test_failed_commit_of_an_mfa_only_reset_is_reported_without_details(capsys) -> None:
    run = _Run("2", commit_error=_StoreDown("UPDATE users SET totp_secret=NULL -- s3cret"))

    with pytest.raises(SystemExit) as exit_info:
        run.run()

    assert exit_info.value.code == 1
    assert run.events[-1] == "rollback"
    out = capsys.readouterr().out
    assert "s3cret" not in out
    assert "MFA disabled" not in out
    assert "Nothing was changed" in out
    assert "signed out" not in out


@pytest.mark.parametrize("choice", ["1", "3"])
def test_failed_session_delete_rolls_back_and_reports_failure(choice: str, capsys) -> None:
    run = _Run(choice, delete_error=_StoreDown("timeout"))

    with pytest.raises(SystemExit) as exit_info:
        run.run()

    assert exit_info.value.code == 1
    run.manager.delete_user_sessions.assert_called_once_with(USER_ID, strict=True)
    assert "commit" not in run.events
    assert run.events[-1] == "rollback"
    out = capsys.readouterr().out
    assert "Password updated" not in out
    assert "MFA disabled" not in out
    assert "Done for" not in out
    assert "rolled back" in out


def test_disable_mfa_only_revokes_nothing(capsys) -> None:
    run = _Run("2")
    run.run()

    # Decision recorded in the module docstring: choice 2 rotates no
    # credential, so it neither needs the session store nor revokes a grant.
    run.manager_cls.assert_not_called()
    run.manager.delete_user_sessions.assert_not_called()
    run.getpass.assert_not_called()
    assert run.statements == []
    assert run.events.count("commit") == 1
    assert run.user.totp_enabled is False and run.user.totp_secret is None
    assert run.user.password_hash == "old-hash"
    out = capsys.readouterr().out
    assert "MFA disabled" in out
    assert "were NOT revoked" in out


def test_the_cli_and_the_service_share_the_revocation_statements() -> None:
    from services import oauth_grant_revocation

    assert (
        reset_password.revoke_oauth_grants_sync is oauth_grant_revocation.revoke_oauth_grants_sync
    )
