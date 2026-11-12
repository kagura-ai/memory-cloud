"""Seam for SYSTEM_ADMIN_MODE (#2767): mode helpers and the first-account role.

``auth.system_admin_mode`` is the one place that says whether ``role='admin'``
grants anything on this process, and ``auth.roles.new_account_role`` the one
place that decides the role of a new account. #1734 (email sign-up) creates
accounts through the same helper.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from auth import system_admin_mode as sam
from auth.roles import Role, RoleManager, new_account_role
from config.settings import get_settings

MODES = ("session", "off", "service")


@pytest.fixture
def mode(monkeypatch):
    def _set(value: str) -> None:
        monkeypatch.setattr(get_settings(), "system_admin_mode", value)

    return _set


def _db_with_user_count(count: int | None) -> MagicMock:
    db = MagicMock()
    result = MagicMock()
    result.scalar.return_value = count
    db.execute = AsyncMock(return_value=result)
    return db


class TestModeHelpers:
    def test_default_mode_is_session(self):
        assert get_settings().system_admin_mode == "session"
        assert sam.system_admin_mode() == "session"

    @pytest.mark.parametrize("value", MODES)
    def test_system_admin_mode_reads_the_setting(self, mode, value):
        mode(value)
        assert sam.system_admin_mode() == value

    @pytest.mark.parametrize(
        ("value", "expected"), [("session", True), ("off", False), ("service", False)]
    )
    def test_session_admin_enabled_only_in_session(self, mode, value, expected):
        mode(value)
        assert sam.session_admin_enabled() is expected

    @pytest.mark.parametrize("value", MODES)
    def test_role_grants_system_admin(self, mode, value):
        mode(value)
        in_session = value == "session"
        assert sam.role_grants_system_admin("admin") is in_session
        assert sam.role_grants_system_admin("user") is False
        assert sam.role_grants_system_admin("operator") is False
        assert sam.role_grants_system_admin(None) is False
        assert sam.role_grants_system_admin("ADMIN") is False

    @pytest.mark.parametrize("value", MODES)
    def test_is_effective_session_admin(self, mode, value):
        mode(value)
        in_session = value == "session"
        assert sam.is_effective_session_admin({"role": "admin"}) is in_session
        assert sam.is_effective_session_admin({"role": "user"}) is False
        assert sam.is_effective_session_admin({}) is False
        assert sam.is_effective_session_admin(None) is False

    @pytest.mark.parametrize("value", MODES)
    def test_first_user_bootstrap_kept_in_every_mode(self, mode, value):
        # Decision O-7: the first sign-up on an empty database is still
        # admitted; outside session it is not promoted (new_account_role).
        mode(value)
        assert sam.first_user_bootstrap_applies() is True

    def test_is_operator_principal_reads_principal_kind_not_role(self):
        assert sam.is_operator_principal({"principal_kind": "operator"}) is True
        assert sam.is_operator_principal({"principal_kind": "session", "role": "operator"}) is False
        assert sam.is_operator_principal({"role": "admin"}) is False
        assert sam.is_operator_principal(None) is False

    def test_reserved_prefixes(self):
        assert sam.OPERATOR_PRINCIPAL_PREFIX == "operator:"
        assert sam.RESERVED_USER_ID_PREFIXES == ("operator:",)


class TestNewAccountRole:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("value", "expected"), [("session", Role.ADMIN), ("off", Role.USER), ("service", Role.USER)]
    )
    async def test_first_account(self, mode, value, expected):
        mode(value)
        db = _db_with_user_count(0)
        assert await new_account_role(db) == expected
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", MODES)
    async def test_later_accounts_are_users(self, mode, value):
        mode(value)
        db = _db_with_user_count(3)
        assert await new_account_role(db) == Role.USER
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_null_count_counts_as_empty(self, mode):
        mode("session")
        assert await new_account_role(_db_with_user_count(None)) == Role.ADMIN


class TestInMemoryRoleManager:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("value", "expected"), [("session", Role.ADMIN), ("off", Role.USER), ("service", Role.USER)]
    )
    async def test_first_in_memory_user(self, mode, value, expected):
        mode(value)
        manager = RoleManager(use_postgres=False)
        assert await manager.ensure_user("first@example.com", user_id="1") == expected
        assert await manager.ensure_user("second@example.com", user_id="2") == Role.USER
