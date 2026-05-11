"""Tests for PermissionService RBAC."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.permission_service import (
    CONTEXT_ROLE_WEIGHTS,
    ORG_ROLE_WEIGHTS,
    CallerId,
    MemoryAuthorId,
    PermissionService,
)
from utils.exceptions import AuthorizationError, NotFoundException


class TestRoleWeights:
    """Test role hierarchy definitions."""

    def test_org_role_hierarchy(self):
        """Owner > admin > member > viewer."""
        assert ORG_ROLE_WEIGHTS["owner"] > ORG_ROLE_WEIGHTS["admin"]
        assert ORG_ROLE_WEIGHTS["admin"] > ORG_ROLE_WEIGHTS["member"]
        assert ORG_ROLE_WEIGHTS["member"] > ORG_ROLE_WEIGHTS["viewer"]

    def test_context_role_hierarchy(self):
        """Owner > editor > viewer."""
        assert CONTEXT_ROLE_WEIGHTS["owner"] > CONTEXT_ROLE_WEIGHTS["editor"]
        assert CONTEXT_ROLE_WEIGHTS["editor"] > CONTEXT_ROLE_WEIGHTS["viewer"]


class TestWorkspaceAccess:
    """Test workspace-level permission checks."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = MagicMock()
        # Mock workspace exists (not deleted)
        mock_ws = MagicMock(id=uuid4(), deleted_at=None)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_ws
        db.execute = AsyncMock(return_value=mock_result)
        return db

    @pytest.fixture
    def service(self, mock_db):
        """Create PermissionService."""
        return PermissionService(mock_db)

    @pytest.mark.asyncio
    async def test_check_workspace_access_owner(self, service):
        """Owner should have access to any role requirement."""
        ws_id = uuid4()
        mock_member = MagicMock(role="owner")
        service.workspace_service.get_member = AsyncMock(return_value=mock_member)

        result = await service.check_workspace_access("user1", ws_id, required_role="owner")
        assert result == mock_member

    @pytest.mark.asyncio
    async def test_check_workspace_access_insufficient_role(self, service):
        """Viewer should not have admin access."""
        ws_id = uuid4()
        mock_member = MagicMock(role="viewer")
        service.workspace_service.get_member = AsyncMock(return_value=mock_member)

        with pytest.raises(AuthorizationError) as exc_info:
            await service.check_workspace_access("user1", ws_id, required_role="admin")
        assert exc_info.value.status_code == 403
        assert exc_info.value.details.get("reason") == "role_too_low"

    @pytest.mark.asyncio
    async def test_check_workspace_access_not_member(self, service):
        """Non-member should be denied."""
        ws_id = uuid4()
        service.workspace_service.get_member = AsyncMock(return_value=None)

        with pytest.raises(AuthorizationError) as exc_info:
            await service.check_workspace_access("user1", ws_id)
        assert exc_info.value.status_code == 403
        assert exc_info.value.details.get("reason") == "not_a_member"

    @pytest.mark.asyncio
    async def test_check_workspace_access_deleted_workspace(self, service, mock_db):
        """Deleted workspace should return 403."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # workspace deleted
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(AuthorizationError) as exc_info:
            await service.check_workspace_access("user1", uuid4())
        assert exc_info.value.status_code == 403
        assert exc_info.value.details.get("reason") == "workspace_deleted"

    @pytest.mark.asyncio
    async def test_is_workspace_member_true(self, service):
        """User with membership returns True."""
        service.workspace_service.get_member = AsyncMock(return_value=MagicMock(role="member"))
        assert await service.is_workspace_member("user1", uuid4()) is True

    @pytest.mark.asyncio
    async def test_is_workspace_member_false(self, service):
        """User without membership returns False."""
        service.workspace_service.get_member = AsyncMock(return_value=None)
        assert await service.is_workspace_member("user1", uuid4()) is False

    @pytest.mark.asyncio
    async def test_check_workspace_owner(self, service):
        """Convenience method for owner check."""
        ws_id = uuid4()
        mock_member = MagicMock(role="owner")
        service.workspace_service.get_member = AsyncMock(return_value=mock_member)
        result = await service.check_workspace_owner("user1", ws_id)
        assert result == mock_member

    @pytest.mark.asyncio
    async def test_check_workspace_admin(self, service):
        """Convenience method for admin check."""
        ws_id = uuid4()
        mock_member = MagicMock(role="admin")
        service.workspace_service.get_member = AsyncMock(return_value=mock_member)
        result = await service.check_workspace_admin("user1", ws_id)
        assert result == mock_member


class TestContextAccess:
    """Test context-level permission checks."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        return MagicMock()

    @pytest.fixture
    def service(self, mock_db):
        return PermissionService(mock_db)

    @pytest.mark.asyncio
    async def test_context_access_private_creator(self, service, mock_db):
        """Creator of private context should have owner access."""
        ctx_id = uuid4()
        mock_ctx = MagicMock(
            id=ctx_id, is_private=True, created_by="user1", workspace_id=uuid4(), deleted_at=None
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_ctx
        mock_db.execute = AsyncMock(return_value=mock_result)

        context, role = await service.check_context_access("user1", ctx_id)
        assert role == "owner"

    @pytest.mark.asyncio
    async def test_context_access_private_non_creator(self, service, mock_db):
        """Non-creator of private context should be denied."""
        ctx_id = uuid4()
        mock_ctx = MagicMock(
            id=ctx_id,
            is_private=True,
            created_by="other_user",
            workspace_id=uuid4(),
            deleted_at=None,
        )
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_ctx
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(AuthorizationError) as exc_info:
            await service.check_context_access("user1", ctx_id)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_context_not_found(self, service, mock_db):
        """Deleted or missing context should return 404."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(NotFoundException) as exc_info:
            await service.check_context_access("user1", uuid4())
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_can_manage_context_true(self, service, mock_db):
        """Owner can manage context."""
        ctx_id = uuid4()
        mock_ctx = MagicMock(id=ctx_id, is_private=True, created_by="user1", deleted_at=None)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_ctx
        mock_db.execute = AsyncMock(return_value=mock_result)

        assert await service.can_manage_context("user1", ctx_id) is True

    @pytest.mark.asyncio
    async def test_can_manage_context_false(self, service, mock_db):
        """Non-owner cannot manage context."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        assert await service.can_manage_context("user1", uuid4()) is False


class TestMemoryAccessControl:
    """Test memory access control for team collaboration."""

    @pytest.fixture
    def service(self):
        return PermissionService(MagicMock())

    @pytest.mark.asyncio
    async def test_owner_can_access_own_memory(self, service):
        """Memory owner always has access."""
        result = await service.can_access_memory(
            user_id=CallerId("user1"),
            memory_user_id=MemoryAuthorId("user1"),
            workspace_id=uuid4(),
            context_id=uuid4(),
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_non_owner_private_context_denied(self, service):
        """Non-owner denied access to private context memory."""
        from unittest.mock import patch

        mock_ctx_svc = MagicMock()
        mock_ctx_svc.is_context_shared = AsyncMock(return_value=False)

        with patch("services.context_service.ContextService", return_value=mock_ctx_svc):
            result = await service.can_access_memory(
                user_id=CallerId("user2"),
                memory_user_id=MemoryAuthorId("user1"),
                workspace_id=uuid4(),
                context_id=uuid4(),
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_workspace_member_shared_context_access(self, service):
        """Workspace member can access shared context memory."""
        from unittest.mock import patch

        mock_ctx_svc = MagicMock()
        mock_ctx_svc.is_context_shared = AsyncMock(return_value=True)
        service.workspace_service.get_member = AsyncMock(return_value=MagicMock(role="member"))

        with patch("services.context_service.ContextService", return_value=mock_ctx_svc):
            result = await service.can_access_memory(
                user_id=CallerId("user2"),
                memory_user_id=MemoryAuthorId("user1"),
                workspace_id=uuid4(),
                context_id=uuid4(),
            )
        assert result is True


class TestCountContextOwners:
    """Test count_context_owners helper (Issue #362)."""

    def _service_with_count(self, count_value: int) -> PermissionService:
        db = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = count_value
        db.execute = AsyncMock(return_value=mock_result)
        return PermissionService(db)

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_owner_row(self):
        service = self._service_with_count(0)
        assert await service.count_context_owners(uuid4()) == 0

    @pytest.mark.asyncio
    async def test_returns_count_when_owners_present(self):
        service = self._service_with_count(3)
        assert await service.count_context_owners(uuid4()) == 3

    @pytest.mark.asyncio
    async def test_handles_none_result_as_zero(self):
        """scalar_one returning None (no rows) should coerce to 0."""
        db = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one.return_value = None
        db.execute = AsyncMock(return_value=mock_result)
        service = PermissionService(db)
        assert await service.count_context_owners(uuid4()) == 0


class TestGetAccessibleContextsForViewer:
    """Issue #398 regression: get_accessible_contexts must reach the viewer
    branch.

    Previously gated at ``required_role="member"``, which raised 403 for
    viewer (weight=1 < member weight=2) — making the explicit viewer branch
    at lines ~588-613 unreachable. UX symptom: viewer's contexts list was
    empty even when shared contexts existed in the workspace.
    """

    @pytest.fixture
    def viewer_member(self):
        member = MagicMock()
        member.role = "viewer"
        member.allowed_context_ids = None  # No restriction → all shared contexts
        return member

    @pytest.fixture
    def service_with_viewer(self, viewer_member):
        db = MagicMock()
        # Workspace lookup (not deleted)
        ws_lookup_result = MagicMock()
        ws_lookup_result.scalar_one_or_none.return_value = MagicMock(id=uuid4(), deleted_at=None)
        # Context list query result
        ctx_list_result = MagicMock()
        ctx_list_result.scalars.return_value.all.return_value = ["ctx-a", "ctx-b"]
        db.execute = AsyncMock(side_effect=[ws_lookup_result, ctx_list_result])
        service = PermissionService(db)
        service.workspace_service.get_member = AsyncMock(return_value=viewer_member)
        return service

    @pytest.mark.asyncio
    async def test_viewer_reaches_viewer_branch(self, service_with_viewer):
        """Viewer must NOT be 403'd by the membership gate before the
        per-role branches run."""
        result = await service_with_viewer.get_accessible_contexts("viewer-user", uuid4())
        assert result == ["ctx-a", "ctx-b"], (
            "viewer should reach the per-role branch and receive shared "
            "contexts; receiving an empty list (or AuthorizationError) means "
            "the membership gate rejected viewer before the branch ran"
        )

    @pytest.mark.asyncio
    async def test_viewer_with_empty_whitelist_returns_empty(self):
        """Viewer with allowed_context_ids=[] → no access (explicit empty)."""
        viewer = MagicMock()
        viewer.role = "viewer"
        viewer.allowed_context_ids = []

        db = MagicMock()
        ws_lookup_result = MagicMock()
        ws_lookup_result.scalar_one_or_none.return_value = MagicMock(id=uuid4(), deleted_at=None)
        ctx_list_result = MagicMock()
        ctx_list_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(side_effect=[ws_lookup_result, ctx_list_result])
        service = PermissionService(db)
        service.workspace_service.get_member = AsyncMock(return_value=viewer)

        result = await service.get_accessible_contexts("viewer-user", uuid4())
        assert result == []


class TestResolveContextWhitelistEnforcement:
    """``resolve_context_for_workspace_read`` must apply the same
    ``allowed_context_ids`` whitelist that ``check_context_access`` enforces.
    Without this, a restricted member/viewer could read UUID-addressed
    endpoints (``/graph/*``) for contexts outside their whitelist.
    """

    def _service_with_member(self, member, context):
        db = MagicMock()
        # First db.execute → context lookup
        ctx_result = MagicMock()
        ctx_result.scalar_one_or_none.return_value = context
        # Second db.execute (inside check_workspace_access) → workspace lookup
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = MagicMock(
            id=context.workspace_id, deleted_at=None
        )
        db.execute = AsyncMock(side_effect=[ctx_result, ws_result])
        service = PermissionService(db)
        service.workspace_service.get_member = AsyncMock(return_value=member)
        return service

    @pytest.mark.asyncio
    async def test_member_outside_whitelist_gets_404(self):
        ctx_id = uuid4()
        ctx = MagicMock(id=ctx_id, workspace_id=uuid4(), is_private=False, created_by="someone")
        member = MagicMock()
        member.role = "member"
        member.allowed_context_ids = [uuid4()]  # whitelist excludes ctx_id
        service = self._service_with_member(member, ctx)

        with pytest.raises(NotFoundException) as exc_info:
            await service.resolve_context_for_workspace_read("user1", ctx_id)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_viewer_outside_whitelist_gets_404(self):
        ctx_id = uuid4()
        ctx = MagicMock(id=ctx_id, workspace_id=uuid4(), is_private=False, created_by="someone")
        viewer = MagicMock()
        viewer.role = "viewer"
        viewer.allowed_context_ids = []  # explicit empty whitelist
        service = self._service_with_member(viewer, ctx)

        with pytest.raises(NotFoundException) as exc_info:
            await service.resolve_context_for_workspace_read("user1", ctx_id)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_member_inside_whitelist_succeeds(self):
        ctx_id = uuid4()
        ctx = MagicMock(id=ctx_id, workspace_id=uuid4(), is_private=False, created_by="someone")
        member = MagicMock()
        member.role = "member"
        member.allowed_context_ids = [ctx_id]  # whitelist includes ctx_id
        service = self._service_with_member(member, ctx)

        result = await service.resolve_context_for_workspace_read("user1", ctx_id)
        assert result is ctx

    @pytest.mark.asyncio
    async def test_admin_bypasses_whitelist(self):
        """Workspace admin/owner should never be filtered by allowed_context_ids
        (matches check_context_access bypass semantics)."""
        ctx_id = uuid4()
        ctx = MagicMock(id=ctx_id, workspace_id=uuid4(), is_private=False, created_by="someone")
        admin = MagicMock()
        admin.role = "admin"
        admin.allowed_context_ids = [uuid4()]  # would exclude ctx_id if checked
        service = self._service_with_member(admin, ctx)

        result = await service.resolve_context_for_workspace_read("user1", ctx_id)
        assert result is ctx

    @pytest.mark.asyncio
    async def test_suspended_member_with_null_whitelist_gets_404(self):
        """Migration 042: a member with allowed_context_ids=NULL is in the
        suspended state and must not reach any UUID-addressed read endpoint.
        get_accessible_contexts returns [] for the same shape; this helper
        must align so /graph/* doesn't become a back door."""
        ctx_id = uuid4()
        ctx = MagicMock(id=ctx_id, workspace_id=uuid4(), is_private=False, created_by="someone")
        suspended_member = MagicMock()
        suspended_member.role = "member"
        suspended_member.allowed_context_ids = None  # suspended
        service = self._service_with_member(suspended_member, ctx)

        with pytest.raises(NotFoundException) as exc_info:
            await service.resolve_context_for_workspace_read("user1", ctx_id)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_viewer_with_null_whitelist_succeeds(self):
        """Viewer NULL whitelist means "no restriction" per Migration 042 —
        unlike the member NULL case (suspended). Viewer should pass."""
        ctx_id = uuid4()
        ctx = MagicMock(id=ctx_id, workspace_id=uuid4(), is_private=False, created_by="someone")
        viewer = MagicMock()
        viewer.role = "viewer"
        viewer.allowed_context_ids = None  # no restriction (all contexts)
        service = self._service_with_member(viewer, ctx)

        result = await service.resolve_context_for_workspace_read("user1", ctx_id)
        assert result is ctx
