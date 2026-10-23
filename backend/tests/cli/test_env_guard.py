"""The admin CLIs run the production secret guard before the database (#2239).

``create_admin``, ``delete_admin``, ``reset_password`` and ``seed_e2e_admin``
open their own engine from ``DATABASE_URL`` and used to skip ``Settings``
entirely, so ``ENVIRONMENT=production`` with a placeholder secret did not stop
them. ``create_admin`` / ``reset_password`` must also resolve
``API_KEY_SECRET`` before the settings are cached: the encryptor reads the key
from them.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cli import (  # noqa: E402
    _env_guard,
    apply_rerank_defaults,
    backfill_deleted_context_files,
    create_admin,
    delete_admin,
    migrate_context_embedding,
    reprocess_erasures,
    reset_password,
    restore_context,
    seed_e2e_admin,
    sweep_orphan_vectors,
    sync_llm_pricing,
    transfer_context_creator,
)
from cli._env_guard import (  # noqa: E402
    API_CONTAINERS,
    get_env_from_api_container,
    require_settings,
    resolve_api_key_secret,
)
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


_UUID = "00000000-0000-0000-0000-000000000001"


@pytest.mark.parametrize(
    ("module", "argv", "get_db"),
    [
        (apply_rerank_defaults, ["--all"], "cli._oneshot.get_db"),
        (backfill_deleted_context_files, [], "cli._oneshot.get_db"),
        (restore_context, [_UUID], "cli._oneshot.get_db"),
        (sweep_orphan_vectors, [], "cli._oneshot.get_db"),
        (
            transfer_context_creator,
            ["--from", "local:x", "--to", _UUID, "--workspace", _UUID],
            "cli._oneshot.get_db",
        ),
        (reprocess_erasures, ["--all"], "cli._oneshot.get_db"),
        (
            migrate_context_embedding,
            ["--context", _UUID, "--to", "any-model"],
            "cli.migrate_context_embedding.get_db",
        ),
        (sync_llm_pricing, [], "cli.sync_llm_pricing.get_db"),
    ],
    ids=[
        "apply_rerank_defaults",
        "backfill_deleted_context_files",
        "restore_context",
        "sweep_orphan_vectors",
        "transfer_context_creator",
        "reprocess_erasures",
        "migrate_context_embedding",
        "sync_llm_pricing",
    ],
)
def test_each_one_shot_cli_stops_before_the_database(module, argv, get_db, refused, capsys) -> None:
    # The one-shot commands used to hit the guard only when their session
    # opened, as "error: 1 validation error ..." or a traceback (#2264).
    with patch(get_db, side_effect=AssertionError("opened the database")) as opened:
        with pytest.raises(SystemExit) as exc_info:
            asyncio.run(module._main(module._parse(argv)))

    assert exc_info.value.code == 1
    opened.assert_not_called()
    err = capsys.readouterr().err
    assert "API_KEY_SECRET" in err
    assert _PLACEHOLDER not in err
    assert "error:" not in err


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
    monkeypatch.setattr(_env_guard, "get_env_from_api_container", lambda key: "container-secret")
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
    assert "API_KEY_SECRET not available" in capsys.readouterr().err


def test_reset_password_resolves_the_container_secret_before_the_settings(monkeypatch) -> None:
    monkeypatch.delenv("API_KEY_SECRET", raising=False)
    monkeypatch.setattr(_env_guard, "get_env_from_api_container", lambda key: "container-secret")
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


def _completed(name: str, returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        ["docker", "exec", name, "printenv", "API_KEY_SECRET"], returncode, stdout, ""
    )


class TestGetEnvFromApiContainer:
    """One lookup for every stack, by the pinned ``container_name`` (#2264)."""

    @pytest.fixture(autouse=True)
    def _real_lookup(self, monkeypatch) -> None:
        # tests/cli/conftest.py stubs the lookup for every CLI test; undo it.
        monkeypatch.setattr(_env_guard, "get_env_from_api_container", get_env_from_api_container)

    def test_the_container_names_of_both_stacks(self) -> None:
        assert API_CONTAINERS == ("kagura-api", "kagura-api-blue", "kagura-api-green")

    def test_tries_each_container_in_order_without_compose(self) -> None:
        with patch(
            "cli._env_guard.subprocess.run",
            side_effect=[
                _completed("kagura-api", returncode=1),
                _completed("kagura-api-blue", stdout="\n"),
                _completed("kagura-api-green", stdout="green-secret\n"),
            ],
        ) as run:
            assert get_env_from_api_container("API_KEY_SECRET") == "green-secret"

        assert [c.args[0] for c in run.call_args_list] == [
            ["docker", "exec", name, "printenv", "API_KEY_SECRET"] for name in API_CONTAINERS
        ]
        for c in run.call_args_list:
            assert "cwd" not in c.kwargs
            assert c.kwargs["timeout"] == 10

    def test_the_one_running_container_answers(self) -> None:
        with patch(
            "cli._env_guard.subprocess.run",
            side_effect=[
                _completed("kagura-api", stdout="dev-secret\n"),
                _completed("kagura-api-blue", returncode=1),
                _completed("kagura-api-green", returncode=1),
            ],
        ):
            assert get_env_from_api_container("API_KEY_SECRET") == "dev-secret"

    def test_both_colors_with_the_same_value(self) -> None:
        with patch(
            "cli._env_guard.subprocess.run",
            side_effect=[
                _completed("kagura-api", returncode=1),
                _completed("kagura-api-blue", stdout="shared\n"),
                _completed("kagura-api-green", stdout="shared\n"),
            ],
        ):
            assert get_env_from_api_container("API_KEY_SECRET") == "shared"

    def test_colors_that_disagree_are_not_guessed_between(self, capsys) -> None:
        # During a deploy both colors run; one may predate an .env.prod edit.
        with patch(
            "cli._env_guard.subprocess.run",
            side_effect=[
                _completed("kagura-api", returncode=1),
                _completed("kagura-api-blue", stdout="old-value\n"),
                _completed("kagura-api-green", stdout="new-value\n"),
            ],
        ):
            assert get_env_from_api_container("API_KEY_SECRET") is None

        err = capsys.readouterr().err
        assert "kagura-api-blue" in err and "kagura-api-green" in err
        assert "API_KEY_SECRET" in err
        assert "old-value" not in err and "new-value" not in err

    @pytest.mark.parametrize(
        "error",
        [FileNotFoundError("docker"), subprocess.TimeoutExpired(["docker"], 10)],
        ids=["no-docker", "timeout"],
    )
    def test_docker_unavailable_stops_after_one_attempt(self, error) -> None:
        # No docker binary or a hung daemon: the other names would fail the
        # same way, so do not wait for each of them.
        with patch("cli._env_guard.subprocess.run", side_effect=error) as run:
            assert get_env_from_api_container("API_KEY_SECRET") is None

        assert run.call_count == 1

    def test_the_environment_wins_without_asking_docker(self, monkeypatch) -> None:
        monkeypatch.setenv("API_KEY_SECRET", "env-secret")
        with patch("cli._env_guard.subprocess.run") as run:
            assert resolve_api_key_secret() == "env-secret"

        run.assert_not_called()

    def test_a_container_hit_is_exported_for_the_settings(self, monkeypatch) -> None:
        import os

        monkeypatch.delenv("API_KEY_SECRET", raising=False)
        with patch(
            "cli._env_guard.subprocess.run",
            return_value=_completed("kagura-api", stdout="container-secret\n"),
        ):
            assert resolve_api_key_secret() == "container-secret"

        assert os.environ["API_KEY_SECRET"] == "container-secret"

    def test_nothing_found_returns_none(self, monkeypatch) -> None:
        import os

        monkeypatch.delenv("API_KEY_SECRET", raising=False)
        with patch("cli._env_guard.subprocess.run", side_effect=FileNotFoundError("docker")):
            assert resolve_api_key_secret() is None

        assert "API_KEY_SECRET" not in os.environ
