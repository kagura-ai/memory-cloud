"""Tests for MemberCredentialsService._check_can_view (Issue #605).

Follow-up to #401 / PR #602 — commit 93c5fd47 swapped ``_check_can_view``
to raise ``AuthorizationError`` instead of ``fastapi.HTTPException`` but
no service-layer unit tests existed. These tests pin the post-refactor
contract:

- self-access bypass (no role lookup)
- owner / admin allow paths
- member / viewer deny paths
- non-member (None role) deny path
- status_code == 403, message == "Insufficient permissions", reason is None
  (single-bit decision — not multi-path like PermissionService.check_workspace_access)
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.member_credentials_service import MemberCredentialsService
from utils.exceptions import AuthorizationError


class TestCheckCanView:
    @pytest.fixture
    def mock_db(self):
        return MagicMock()

    @pytest.fixture
    def service(self, mock_db):
        return MemberCredentialsService(mock_db)

    @pytest.fixture
    def workspace_id(self):
        return uuid4()

    @pytest.mark.asyncio
    async def test_self_access_allowed(self, service, workspace_id):
        """Self-access short-circuits before any role lookup runs."""
        await service._check_can_view("user1", workspace_id, "user1")

    @pytest.mark.asyncio
    async def test_self_access_does_not_query_role(self, service, workspace_id):
        """Self-access must NOT call get_workspace_role — if it did, a future
        regression that adds DB-bound side effects to the role lookup would
        silently break self-only callers."""
        service.get_workspace_role = AsyncMock(return_value=None)
        await service._check_can_view("user1", workspace_id, "user1")
        service.get_workspace_role.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_workspace_owner_allowed_to_view_other(self, service, workspace_id):
        service.get_workspace_role = AsyncMock(return_value="owner")
        await service._check_can_view("owner_user", workspace_id, "target_user")

    @pytest.mark.asyncio
    async def test_workspace_admin_allowed_to_view_other(self, service, workspace_id):
        service.get_workspace_role = AsyncMock(return_value="admin")
        await service._check_can_view("admin_user", workspace_id, "target_user")

    @pytest.mark.asyncio
    async def test_workspace_member_denied(self, service, workspace_id):
        service.get_workspace_role = AsyncMock(return_value="member")
        with pytest.raises(AuthorizationError) as exc_info:
            await service._check_can_view("member_user", workspace_id, "target_user")
        assert exc_info.value.status_code == 403
        assert exc_info.value.message == "Insufficient permissions"
        # Single-bit decision (owner/admin vs everyone else) — no multi-path
        # classification, unlike PermissionService.check_workspace_access which
        # carries reason ∈ {workspace_deleted, not_a_member, role_too_low}.
        assert exc_info.value.reason is None

    @pytest.mark.asyncio
    async def test_workspace_viewer_denied(self, service, workspace_id):
        service.get_workspace_role = AsyncMock(return_value="viewer")
        with pytest.raises(AuthorizationError) as exc_info:
            await service._check_can_view("viewer_user", workspace_id, "target_user")
        assert exc_info.value.status_code == 403
        assert exc_info.value.message == "Insufficient permissions"
        assert exc_info.value.reason is None

    @pytest.mark.asyncio
    async def test_non_member_denied(self, service, workspace_id):
        """get_workspace_role returns None for a user with no workspace
        membership — must fall through to deny."""
        service.get_workspace_role = AsyncMock(return_value=None)
        with pytest.raises(AuthorizationError) as exc_info:
            await service._check_can_view("outsider", workspace_id, "target_user")
        assert exc_info.value.status_code == 403
