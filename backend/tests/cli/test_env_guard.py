"""The admin CLIs run the production secret guard before the database (#2239).

``create_admin``, ``delete_admin``, ``reset_password`` and ``seed_e2e_admin``
open their own engine from ``DATABASE_URL`` and used to skip ``Settings``
entirely, so ``ENVIRONMENT=production`` with a placeholder secret did not stop
them. ``create_admin`` / ``reset_password`` must also resolve
``API_KEY_SECRET`` before the settings are cached: the encryptor reads the key
from them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cli import create_admin, delete_admin, reset_password, seed_e2e_admin  # noqa: E402
from cli._env_guard import require_settings  # noqa: E402
from config.settings import Settings  # noqa: E402

_PLACEHOLDER = "change-me-api-key-secret"


class _Stop(Exception):
    """Raised by a patched engine factory: the CLI got past the guard."""


def _guard_refusal() -> ValidationError:
    """The guard's real error, for a production install with one placeholder."""
    values = {
        "api_key_secret": _PLACEHOLDER,
        "audit_hmac_key": "k" * 64,
        "audit_pseudo_salt": "private-salt",
        "environment": "production",
    }
    try:
        Settings(_env_file=None, **values)
    except ValidationError as exc:
        return exc
    raise AssertionError("the guard did not refuse a placeholder")


@pytest.fixture
def refused(monkeypatch) -> None:
    """``get_settings()`` raises what the guard raises in production."""
    error = _guard_refusal()

    def _raise() -> Settings:
        raise error

    monkeypatch.setattr("config.settings.get_settings", _raise)


class TestRequireSettings:
    def test_refusal_exits_1_naming_the_variable_not_the_value(self, refused, capsys) -> None:
        with pytest.raises(SystemExit) as exc_info:
            require_settings()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "API_KEY_SECRET" in err
        assert "docs/deployment.md" in err
        assert _PLACEHOLDER not in err
        assert "Traceback" not in err

    def test_returns_the_cached_settings(self) -> None:
        from config.settings import get_settings

        assert require_settings() is get_settings()

    def test_a_secret_other_than_the_cached_one_is_refused(self, capsys) -> None:
        with pytest.raises(SystemExit) as exc_info:
            require_settings(api_key_secret="resolved-after-the-settings-were-cached")

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "API_KEY_SECRET changed" in err
        assert "resolved-after" not in err


@pytest.mark.parametrize(
    ("module", "entry"),
    [
        (create_admin, lambda: create_admin.create_admin(skip_mcp_json=True)),
        (delete_admin, delete_admin.delete_admin),
        (reset_password, reset_password.reset_password),
        (seed_e2e_admin, seed_e2e_admin.seed_e2e_admin),
    ],
    ids=["create_admin", "delete_admin", "reset_password", "seed_e2e_admin"],
)
def test_each_cli_stops_before_the_database(module, entry, refused, monkeypatch, capsys) -> None:
    monkeypatch.setenv("E2E_ADMIN_LOGIN_ID", "e2e-admin")
    monkeypatch.setenv("E2E_ADMIN_PASSWORD", "Valid-passw0rd!")

    with (
        patch.object(module, "create_engine") as engine,
        patch("builtins.input", side_effect=AssertionError("prompted before the guard")),
    ):
        with pytest.raises(SystemExit) as exc_info:
            entry()

    assert exc_info.value.code == 1
    engine.assert_not_called()
    assert "API_KEY_SECRET" in capsys.readouterr().err


def _record_settings(monkeypatch) -> list[str | None]:
    """Patch ``get_settings`` to record ``API_KEY_SECRET`` when it is first built."""
    import os

    seen: list[str | None] = []

    def _get_settings() -> SimpleNamespace:
        seen.append(os.environ.get("API_KEY_SECRET"))
        return SimpleNamespace(api_key_secret=seen[0])

    monkeypatch.setattr("config.settings.get_settings", _get_settings)
    return seen


def test_create_admin_resolves_the_container_secret_before_the_settings(monkeypatch) -> None:
    monkeypatch.delenv("API_KEY_SECRET", raising=False)
    monkeypatch.setattr(create_admin, "_get_env_from_docker", lambda key: "container-secret")
    seen = _record_settings(monkeypatch)

    with patch.object(create_admin, "create_engine", side_effect=_Stop):
        with pytest.raises(_Stop):
            create_admin.create_admin(skip_mcp_json=True)

    assert seen == ["container-secret"]


def test_create_admin_without_a_secret_exits_before_the_settings(monkeypatch, capsys) -> None:
    monkeypatch.delenv("API_KEY_SECRET", raising=False)
    seen = _record_settings(monkeypatch)

    with patch.object(create_admin, "create_engine") as engine:
        with pytest.raises(SystemExit) as exc_info:
            create_admin.create_admin(skip_mcp_json=True)

    assert exc_info.value.code == 1
    assert seen == []
    engine.assert_not_called()
    assert "API_KEY_SECRET not available" in capsys.readouterr().out


def test_reset_password_resolves_the_container_secret_before_the_settings(monkeypatch) -> None:
    monkeypatch.delenv("API_KEY_SECRET", raising=False)
    monkeypatch.setattr(reset_password, "_get_env_from_docker", lambda key: "container-secret")
    seen = _record_settings(monkeypatch)

    with patch.object(reset_password, "create_engine", side_effect=_Stop):
        with pytest.raises(_Stop):
            reset_password.reset_password()

    assert seen == ["container-secret"]


def test_reset_password_without_a_secret_still_runs(monkeypatch) -> None:
    # Only the optional MFA re-enable needs the key.
    monkeypatch.delenv("API_KEY_SECRET", raising=False)
    seen = _record_settings(monkeypatch)

    with patch.object(reset_password, "create_engine", side_effect=_Stop):
        with pytest.raises(_Stop):
            reset_password.reset_password()

    assert seen == [None]


def test_reset_password_does_not_re_enable_mfa_under_a_stale_key(monkeypatch, capsys) -> None:
    # The key in the environment is not the one the cached settings hold:
    # encrypting the TOTP secret now would lock the owner out.
    monkeypatch.setattr(
        "config.settings.get_settings", lambda: SimpleNamespace(api_key_secret="cached-key")
    )
    monkeypatch.setenv("API_KEY_SECRET", "other-key")
    user = MagicMock(totp_enabled=True, user_id="u1")
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = user
    session = MagicMock()
    session.return_value.__enter__.return_value = db

    with (
        patch.object(reset_password, "create_engine"),
        patch.object(reset_password, "get_sync_database_url", return_value="x"),
        patch.object(reset_password, "Session", session),
        patch("builtins.input", side_effect=["admin", "2", "y"]),
    ):
        reset_password.reset_password()

    assert user.totp_enabled is False
    assert user.totp_secret is None
    assert "API_KEY_SECRET changed" in capsys.readouterr().out
