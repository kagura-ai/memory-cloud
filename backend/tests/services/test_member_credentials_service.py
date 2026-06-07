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

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from models.schemas import MemberAPIKeyResponse
from services.member_credentials_service import MemberCredentialsService
from utils.datetime import to_utc_iso
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
        assert exc_info.value.message == "Insufficient permissions"
        assert exc_info.value.reason is None


class TestSerializeApiKeyLastUsed:
    """Issue #943 — the API Keys table surfaces a 'Last used' column, so the
    member-credentials DTO must carry ``last_used_at``. ``api_keys.last_used_at``
    is already tracked on every auth (auth/api_keys.py) — these tests pin that
    the field flows through _serialize_api_key AND survives the response schema
    (a missing schema field would be silently dropped by pydantic at the route)."""

    @pytest.fixture
    def service(self):
        return MemberCredentialsService(MagicMock())

    def _make_key(self, last_used_at):
        # hidden_at set → is_visible False → plaintext path skipped (no encryptor).
        return SimpleNamespace(
            id=1,
            name="prod-api",
            key_prefix="kagura_abc123",
            plaintext_encrypted=None,
            hidden_at=datetime(2020, 1, 1, 0, 0, 0),
            visibility_expires_at=None,
            created_at=datetime(2026, 1, 1, 12, 0, 0),
            revoked_at=None,
            last_used_at=last_used_at,
            bound_context_id=None,
        )

    def test_serialize_includes_last_used_at_when_present(self, service):
        used = datetime(2026, 6, 1, 8, 30, 0)
        out = service._serialize_api_key(self._make_key(used), show_secret=False)
        assert out["last_used_at"] == to_utc_iso(used)

    def test_serialize_last_used_at_null(self, service):
        out = service._serialize_api_key(self._make_key(None), show_secret=False)
        assert out["last_used_at"] is None

    def test_response_schema_preserves_last_used_at(self, service):
        used = datetime(2026, 6, 1, 8, 30, 0)
        out = service._serialize_api_key(self._make_key(used), show_secret=False)
        # Route coerces the dict into MemberAPIKeyResponse — the field must exist
        # on the schema or it is dropped before reaching the client.
        model = MemberAPIKeyResponse(**out)
        assert model.last_used_at == to_utc_iso(used)
