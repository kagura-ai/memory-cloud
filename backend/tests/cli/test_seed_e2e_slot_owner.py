"""``seed_e2e_admin --slot-owner``: the workspace management fixture (#2918).

The option dispatch and the refusals before any database work. What the seed
writes (one user, three Free workspaces, two suspended, the selection
required, idempotent) is pinned on real rows in
``tests/integration/test_seed_e2e_slot_owner_db.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cli import seed_e2e_admin  # noqa: E402


def test_slot_owner_option_seeds_the_slot_owner_only() -> None:
    with (
        patch.object(seed_e2e_admin, "seed_e2e_slot_owner") as slot_owner,
        patch.object(seed_e2e_admin, "seed_e2e_admin") as admin,
    ):
        seed_e2e_admin.main(["--slot-owner"])
    slot_owner.assert_called_once_with()
    admin.assert_not_called()


def test_without_the_option_the_admin_is_seeded_as_before() -> None:
    with (
        patch.object(seed_e2e_admin, "seed_e2e_slot_owner") as slot_owner,
        patch.object(seed_e2e_admin, "seed_e2e_admin") as admin,
    ):
        seed_e2e_admin.main([])
    admin.assert_called_once_with()
    slot_owner.assert_not_called()


@pytest.mark.parametrize(
    ("login", "password", "reason"),
    [
        ("", "Valid-passw0rd!", "must be set"),
        ("e2e-slot-owner", "", "must be set"),
        ("e2e-slot-owner", "x" * 73, "E2E_SLOT_OWNER_PASSWORD"),
    ],
    ids=["no-login", "no-password", "too-long"],
)
def test_bad_credentials_exit_before_the_guard_and_the_database(
    monkeypatch, capsys, login, password, reason
) -> None:
    monkeypatch.setenv("E2E_SLOT_OWNER_LOGIN_ID", login)
    monkeypatch.setenv("E2E_SLOT_OWNER_PASSWORD", password)
    with (
        patch.object(seed_e2e_admin, "require_settings") as guard,
        patch.object(seed_e2e_admin, "get_db") as get_db,
    ):
        with pytest.raises(SystemExit) as exc_info:
            seed_e2e_admin.seed_e2e_slot_owner()
    assert exc_info.value.code == 1
    assert reason in capsys.readouterr().out
    guard.assert_not_called()
    get_db.assert_not_called()


def test_the_secret_guard_runs_before_the_database(monkeypatch) -> None:
    monkeypatch.setenv("E2E_SLOT_OWNER_LOGIN_ID", "e2e-slot-owner")
    monkeypatch.setenv("E2E_SLOT_OWNER_PASSWORD", "Valid-passw0rd!")
    refusal = MagicMock(side_effect=SystemExit(1))
    with (
        patch.object(seed_e2e_admin, "require_settings", refusal),
        patch.object(seed_e2e_admin, "get_db") as get_db,
    ):
        with pytest.raises(SystemExit):
            seed_e2e_admin.seed_e2e_slot_owner()
    refusal.assert_called_once_with()
    get_db.assert_not_called()


def test_the_admin_seed_reads_its_own_variables(monkeypatch, capsys) -> None:
    """The shared credential check still names the admin's variables."""
    monkeypatch.delenv("E2E_ADMIN_LOGIN_ID", raising=False)
    monkeypatch.setenv("E2E_SLOT_OWNER_LOGIN_ID", "e2e-slot-owner")
    monkeypatch.setenv("E2E_SLOT_OWNER_PASSWORD", "Valid-passw0rd!")
    with pytest.raises(SystemExit):
        seed_e2e_admin.seed_e2e_admin()
    assert "E2E_ADMIN_LOGIN_ID and E2E_ADMIN_PASSWORD must be set" in capsys.readouterr().out
