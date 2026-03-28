"""Tests for RoleManager auth_provider tracking.

Issue #361: Track user authentication provider.
"""

import pytest

from auth.roles import Role, RoleManager


class TestRoleManagerAuthProvider:
    """Test auth_provider tracking in RoleManager (in-memory backend)."""

    @pytest.fixture
    def role_manager(self):
        return RoleManager(use_postgres=False)

    @pytest.mark.asyncio
    async def test_first_user_gets_admin(self, role_manager):
        """First user is automatically assigned ADMIN role."""
        role = await role_manager.ensure_user("admin@example.com", "sub-1")
        assert role == Role.ADMIN

    @pytest.mark.asyncio
    async def test_second_user_gets_user(self, role_manager):
        """Subsequent users get USER role."""
        await role_manager.ensure_user("admin@example.com", "sub-1")
        role = await role_manager.ensure_user("user@example.com", "sub-2")
        assert role == Role.USER

    @pytest.mark.asyncio
    async def test_existing_user_returns_role(self, role_manager):
        """Re-login returns existing role without changing it."""
        await role_manager.ensure_user("admin@example.com", "sub-1")
        role = await role_manager.ensure_user("admin@example.com", "sub-1")
        assert role == Role.ADMIN

    @pytest.mark.asyncio
    async def test_has_role_hierarchy(self, role_manager):
        """ADMIN has USER-level access, USER doesn't have ADMIN access."""
        await role_manager.ensure_user("admin@example.com", "sub-1")
        await role_manager.ensure_user("user@example.com", "sub-2")

        assert await role_manager.has_role("admin@example.com", Role.USER) is True
        assert await role_manager.has_role("user@example.com", Role.ADMIN) is False

    @pytest.mark.asyncio
    async def test_assign_role(self, role_manager):
        """Role assignment updates user's role."""
        await role_manager.ensure_user("user@example.com", "sub-1")
        await role_manager.ensure_user("user2@example.com", "sub-2")

        await role_manager.assign_role("user2@example.com", Role.ADMIN)
        assert await role_manager.get_role("user2@example.com") == Role.ADMIN

    @pytest.mark.asyncio
    async def test_assign_role_nonexistent_user(self, role_manager):
        """Assigning role to nonexistent user raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            await role_manager.assign_role("nobody@example.com", Role.ADMIN)
