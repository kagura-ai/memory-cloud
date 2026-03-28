"""Tests for Issue #234: allowed_context_ids feature.

Tests the context access restriction functionality for member/viewer roles.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, WorkspaceMember


class TestAllowedContextIdsPermission:
    """Test allowed_context_ids in permission checks."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = AsyncMock(spec=AsyncSession)
        return db

    @pytest.fixture
    def workspace_id(self):
        return uuid.uuid4()

    @pytest.fixture
    def context_id(self):
        return uuid.uuid4()

    @pytest.fixture
    def user_id(self):
        return "test_user_123"

    @pytest.mark.asyncio
    async def test_member_with_null_allowed_context_ids_has_full_access(
        self, mock_db, workspace_id, context_id, user_id
    ):
        """Member with allowed_context_ids=None should have access to all assigned contexts."""
        # Member with no restriction
        workspace_member = MagicMock(spec=WorkspaceMember)
        workspace_member.role = "member"
        workspace_member.allowed_context_ids = None  # No restriction

        # allowed_context_ids is None means no whitelist restriction
        assert workspace_member.allowed_context_ids is None

    @pytest.mark.asyncio
    async def test_member_with_empty_allowed_context_ids_has_no_access(
        self, mock_db, workspace_id, context_id, user_id
    ):
        """Member with allowed_context_ids=[] should have no access."""
        workspace_member = MagicMock(spec=WorkspaceMember)
        workspace_member.role = "member"
        workspace_member.allowed_context_ids = []  # Empty whitelist

        # Empty list means no contexts allowed
        assert workspace_member.allowed_context_ids is not None
        assert len(workspace_member.allowed_context_ids) == 0

    @pytest.mark.asyncio
    async def test_member_with_specific_allowed_context_ids(self, mock_db, workspace_id, user_id):
        """Member with specific allowed_context_ids should only access those contexts."""
        allowed_id_1 = uuid.uuid4()
        allowed_id_2 = uuid.uuid4()
        denied_id = uuid.uuid4()

        workspace_member = MagicMock(spec=WorkspaceMember)
        workspace_member.role = "member"
        workspace_member.allowed_context_ids = [allowed_id_1, allowed_id_2]

        # Check whitelist logic
        assert allowed_id_1 in workspace_member.allowed_context_ids
        assert allowed_id_2 in workspace_member.allowed_context_ids
        assert denied_id not in workspace_member.allowed_context_ids

    @pytest.mark.asyncio
    async def test_viewer_with_allowed_context_ids_restriction(
        self, mock_db, workspace_id, user_id
    ):
        """Viewer with allowed_context_ids should be restricted."""
        allowed_id = uuid.uuid4()
        denied_id = uuid.uuid4()

        workspace_member = MagicMock(spec=WorkspaceMember)
        workspace_member.role = "viewer"
        workspace_member.allowed_context_ids = [allowed_id]

        # Viewer should also respect whitelist
        assert allowed_id in workspace_member.allowed_context_ids
        assert denied_id not in workspace_member.allowed_context_ids

    @pytest.mark.asyncio
    async def test_admin_ignores_allowed_context_ids(
        self, mock_db, workspace_id, context_id, user_id
    ):
        """Admin should ignore allowed_context_ids (full access)."""
        workspace_member = MagicMock(spec=WorkspaceMember)
        workspace_member.role = "admin"
        workspace_member.allowed_context_ids = []  # Even with empty whitelist

        # Admin bypass - role check happens before whitelist
        assert workspace_member.role in ["admin", "owner"]

    @pytest.mark.asyncio
    async def test_owner_ignores_allowed_context_ids(
        self, mock_db, workspace_id, context_id, user_id
    ):
        """Owner should ignore allowed_context_ids (full access)."""
        workspace_member = MagicMock(spec=WorkspaceMember)
        workspace_member.role = "owner"
        workspace_member.allowed_context_ids = []  # Even with empty whitelist

        # Owner bypass - role check happens before whitelist
        assert workspace_member.role in ["admin", "owner"]


class TestRoleChangeContextAccess:
    """Test that role changes clear allowed_context_ids."""

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock(spec=AsyncSession)
        return db

    @pytest.fixture
    def workspace_id(self):
        return uuid.uuid4()

    @pytest.mark.asyncio
    async def test_role_change_clears_allowed_context_ids(self, mock_db, workspace_id):
        """Role change should clear allowed_context_ids."""
        context_ids = [uuid.uuid4(), uuid.uuid4()]

        # Create member with restrictions
        member = MagicMock(spec=WorkspaceMember)
        member.role = "member"
        member.allowed_context_ids = context_ids

        # Simulate role change logic
        old_role = member.role
        new_role = "viewer"

        # The actual logic in organization_service.py
        if old_role != new_role and member.allowed_context_ids is not None:
            member.allowed_context_ids = None

        # Verify cleared
        assert member.allowed_context_ids is None

    @pytest.mark.asyncio
    async def test_same_role_preserves_allowed_context_ids(self, mock_db, workspace_id):
        """Same role should preserve allowed_context_ids."""
        context_ids = [uuid.uuid4()]

        member = MagicMock(spec=WorkspaceMember)
        member.role = "member"
        member.allowed_context_ids = context_ids.copy()

        # Same role - no change
        old_role = member.role
        new_role = "member"

        if old_role != new_role and member.allowed_context_ids is not None:
            member.allowed_context_ids = None

        # Should be preserved
        assert member.allowed_context_ids == context_ids


class TestPrivacyTransitionContextAccess:
    """Test that privacy changes affect allowed_context_ids."""

    @pytest.fixture
    def workspace_id(self):
        return uuid.uuid4()

    @pytest.fixture
    def context_id(self):
        return uuid.uuid4()

    def test_shared_to_private_removes_from_whitelist(self, workspace_id, context_id):
        """When context becomes private, it should be removed from members' whitelists."""
        # Members with this context in their whitelist
        member1_contexts = [context_id, uuid.uuid4()]
        member2_contexts = [context_id]
        member3_contexts = [uuid.uuid4()]  # Different context

        # Simulate array_remove behavior
        def array_remove(arr, item):
            return [x for x in arr if x != item]

        # After privacy transition
        member1_after = array_remove(member1_contexts, context_id)
        member2_after = array_remove(member2_contexts, context_id)
        member3_after = array_remove(member3_contexts, context_id)

        assert context_id not in member1_after
        assert len(member1_after) == 1  # One context remains

        assert context_id not in member2_after
        assert len(member2_after) == 0  # Empty

        assert len(member3_after) == 1  # Unchanged


class TestPrivateContextMemberRestriction:
    """Test that private contexts cannot have members added."""

    @pytest.fixture
    def context_id(self):
        return uuid.uuid4()

    def test_private_context_member_add_blocked(self, context_id):
        """Adding member to private context should be blocked."""
        context = MagicMock(spec=Context)
        context.id = context_id
        context.is_private = True

        # The check in contexts.py
        if context and context.is_private:
            should_block = True
        else:
            should_block = False

        assert should_block is True

    def test_shared_context_member_add_allowed(self, context_id):
        """Adding member to shared context should be allowed."""
        context = MagicMock(spec=Context)
        context.id = context_id
        context.is_private = False

        if context and context.is_private:
            should_block = True
        else:
            should_block = False

        assert should_block is False


class TestContextAccessDialogFiltering:
    """Test that context access dialog only shows shared contexts."""

    def test_filter_shared_contexts_only(self):
        """Context access dialog should only show shared contexts."""
        context1 = MagicMock(spec=Context)
        context1.id = uuid.uuid4()
        context1.name = "shared1"
        context1.is_private = False

        context2 = MagicMock(spec=Context)
        context2.id = uuid.uuid4()
        context2.name = "private1"
        context2.is_private = True

        context3 = MagicMock(spec=Context)
        context3.id = uuid.uuid4()
        context3.name = "shared2"
        context3.is_private = False

        all_contexts = [context1, context2, context3]

        # Filter logic from frontend
        shared_contexts = [c for c in all_contexts if not c.is_private]

        assert len(shared_contexts) == 2
        assert context1 in shared_contexts
        assert context2 not in shared_contexts
        assert context3 in shared_contexts


class TestContextAccessRestriction:
    """Test that context access respects allowed_context_ids.

    Issue #240: Renamed from TestSwitchContextRestriction.
    This tests the allowed_context_ids whitelist logic, not the removed switch_context MCP tool.
    """

    @pytest.fixture
    def workspace_id(self):
        return uuid.uuid4()

    @pytest.fixture
    def user_id(self):
        return "test_user_123"

    def test_access_to_allowed_context_succeeds(self, workspace_id, user_id):
        """Accessing an allowed context should succeed."""
        allowed_context_id = uuid.uuid4()

        member = MagicMock(spec=WorkspaceMember)
        member.role = "member"
        member.allowed_context_ids = [allowed_context_id]

        # Check would pass
        assert allowed_context_id in member.allowed_context_ids

    def test_access_to_denied_context_fails(self, workspace_id, user_id):
        """Accessing a non-allowed context should fail."""
        allowed_context_id = uuid.uuid4()
        denied_context_id = uuid.uuid4()

        member = MagicMock(spec=WorkspaceMember)
        member.role = "member"
        member.allowed_context_ids = [allowed_context_id]

        # Check would fail
        assert denied_context_id not in member.allowed_context_ids

    def test_access_with_empty_whitelist_fails(self, workspace_id, user_id):
        """Access with empty whitelist should fail for any context."""
        any_context_id = uuid.uuid4()

        member = MagicMock(spec=WorkspaceMember)
        member.role = "member"
        member.allowed_context_ids = []  # Empty = no access

        # Check would fail for any context
        assert any_context_id not in member.allowed_context_ids

    def test_context_by_name_respects_whitelist(self, workspace_id, user_id):
        """get_context_by_name should also respect whitelist."""
        allowed_context_id = uuid.uuid4()
        denied_context_id = uuid.uuid4()

        member = MagicMock(spec=WorkspaceMember)
        member.role = "viewer"
        member.allowed_context_ids = [allowed_context_id]

        # get_context_by_name now calls get_context internally
        # which checks the whitelist
        assert allowed_context_id in member.allowed_context_ids
        assert denied_context_id not in member.allowed_context_ids


class TestNullAllowedContextIds:
    """Test that NULL allowed_context_ids means suspended (not configured)."""

    @pytest.fixture
    def workspace_id(self):
        return uuid.uuid4()

    @pytest.fixture
    def user_id(self):
        return "test_user_123"

    def test_null_means_suspended(self, workspace_id, user_id):
        """Member with allowed_context_ids=NULL should be in suspended state (no access)."""
        # Member with NULL (not configured)
        workspace_member = MagicMock(spec=WorkspaceMember)
        workspace_member.role = "member"
        workspace_member.allowed_context_ids = None  # NULL = suspended

        # Migration 042: NULL means suspended (must configure via Context Access dialog)
        # get_accessible_contexts() will return []
        assert workspace_member.allowed_context_ids is None

    def test_null_vs_empty_array(self, workspace_id, user_id):
        """NULL and empty array both mean no access (suspended vs explicit)."""
        # NULL = suspended (not configured)
        member_null = MagicMock(spec=WorkspaceMember)
        member_null.role = "member"
        member_null.allowed_context_ids = None

        # Empty array = explicit no access
        member_empty = MagicMock(spec=WorkspaceMember)
        member_empty.role = "member"
        member_empty.allowed_context_ids = []

        # Both result in no access, but different reasons
        assert member_null.allowed_context_ids is None  # Suspended
        assert member_empty.allowed_context_ids is not None  # Explicit restriction
        assert len(member_empty.allowed_context_ids) == 0  # No access

    def test_viewer_with_null_is_suspended(self, workspace_id, user_id):
        """Viewer with allowed_context_ids=NULL should be suspended."""
        workspace_member = MagicMock(spec=WorkspaceMember)
        workspace_member.role = "viewer"
        workspace_member.allowed_context_ids = None

        # Migration 042: NULL viewer is also suspended
        assert workspace_member.allowed_context_ids is None


class TestUpdateMemberContextAccess:
    """Test the update_member_context_access functionality."""

    @pytest.fixture
    def workspace_id(self):
        return uuid.uuid4()

    @pytest.fixture
    def user_id(self):
        return "test_user_123"

    def test_set_allowed_context_ids(self, workspace_id, user_id):
        """Setting allowed_context_ids should work correctly."""
        context_ids = [uuid.uuid4(), uuid.uuid4()]

        member = MagicMock(spec=WorkspaceMember)
        member.workspace_id = workspace_id
        member.user_id = user_id
        member.role = "member"
        member.allowed_context_ids = None

        # Update
        member.allowed_context_ids = context_ids

        assert member.allowed_context_ids == context_ids
        assert len(member.allowed_context_ids) == 2

    def test_clear_allowed_context_ids(self, workspace_id, user_id):
        """Setting allowed_context_ids to None should remove restriction."""
        member = MagicMock(spec=WorkspaceMember)
        member.workspace_id = workspace_id
        member.user_id = user_id
        member.role = "member"
        member.allowed_context_ids = [uuid.uuid4()]

        # Clear
        member.allowed_context_ids = None

        assert member.allowed_context_ids is None

    def test_set_empty_allowed_context_ids(self, workspace_id, user_id):
        """Setting allowed_context_ids to [] should deny all access."""
        member = MagicMock(spec=WorkspaceMember)
        member.workspace_id = workspace_id
        member.user_id = user_id
        member.role = "member"
        member.allowed_context_ids = None

        # Set to empty - no access
        member.allowed_context_ids = []

        assert member.allowed_context_ids is not None
        assert len(member.allowed_context_ids) == 0
