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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
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

    @pytest.mark.asyncio
    async def test_threaded_role_skips_lookup(self, service, workspace_id):
        """A caller-supplied requester_role is trusted and skips the duplicate
        WorkspaceMember SELECT — the programmatic owner-key GET path already
        resolved the role via authorize_workspace_management (#1171 cleanup)."""
        service.get_workspace_role = AsyncMock(return_value=None)
        await service._check_can_view(
            "owner_user", workspace_id, "target_user", requester_role="owner"
        )
        service.get_workspace_role.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_threaded_role_still_enforces_deny(self, service, workspace_id):
        """A threaded non-owner/admin role is still denied without a lookup —
        trusting the role must not bypass the owner/admin gate."""
        service.get_workspace_role = AsyncMock(return_value="owner")  # would allow if queried
        with pytest.raises(AuthorizationError) as exc_info:
            await service._check_can_view(
                "member_user", workspace_id, "target_user", requester_role="member"
            )
        assert exc_info.value.status_code == 403
        service.get_workspace_role.assert_not_awaited()


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
            public_id="akey_" + "1" * 22,
            name="prod-api",
            key_prefix="kagura_abc123",
            plaintext_encrypted=None,
            hidden_at=datetime(2020, 1, 1, 0, 0, 0),
            visibility_expires_at=None,
            created_at=datetime(2026, 1, 1, 12, 0, 0),
            revoked_at=None,
            expires_at=None,
            last_used_at=last_used_at,
            bound_context_id=None,
            agent_id=None,
            default_context_id=None,
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


class TestDefaultContextOnMemberKeys:
    """#2188 — the member list carries each key's MCP default context (id +
    name read as the requester) and the agent-bound marker, so the UI needs no
    second fetch of GET /config/api-keys."""

    CONTEXT_ID = uuid4()
    AGENT_ID = uuid4()
    WORKSPACE_ID = uuid4()

    def _key(self, key_id: int, **over):
        base = {
            "id": key_id,
            "public_id": "akey_" + str(key_id) * 22,
            "name": f"key-{key_id}",
            "key_prefix": "kagura_abc123",
            "plaintext_encrypted": None,
            "hidden_at": datetime(2020, 1, 1, 0, 0, 0),
            "visibility_expires_at": None,
            "created_at": datetime(2026, 1, 1, 12, 0, 0),
            "revoked_at": None,
            "expires_at": None,
            "last_used_at": None,
            "bound_context_id": None,
            "agent_id": None,
            "default_context_id": None,
            "workspace_id": self.WORKSPACE_ID,
        }
        base.update(over)
        return SimpleNamespace(**base)

    def _service(self, keys):
        db = MagicMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=lambda: keys)))
        )
        service = MemberCredentialsService(db)
        service._check_can_view = AsyncMock()
        return service

    def test_serialize_emits_default_context_and_agent(self):
        service = MemberCredentialsService(MagicMock())
        key = self._key(1, default_context_id=self.CONTEXT_ID, agent_id=None)
        out = service._serialize_api_key(key, show_secret=False, default_context_name="alpha")
        assert out["default_context_id"] == str(self.CONTEXT_ID)
        assert out["default_context_name"] == "alpha"
        assert out["agent_id"] is None
        model = MemberAPIKeyResponse(**out)
        assert model.default_context_id == str(self.CONTEXT_ID)
        assert model.default_context_name == "alpha"
        assert model.agent_id is None

    def test_serialize_emits_agent_id_and_null_defaults(self):
        service = MemberCredentialsService(MagicMock())
        out = service._serialize_api_key(self._key(2, agent_id=self.AGENT_ID), show_secret=False)
        assert out["agent_id"] == str(self.AGENT_ID)
        assert out["default_context_id"] is None
        assert out["default_context_name"] is None
        assert MemberAPIKeyResponse(**out).agent_id == str(self.AGENT_ID)

    @pytest.mark.asyncio
    async def test_names_are_read_once_per_context_as_the_requester(self):
        keys = [
            self._key(1, default_context_id=self.CONTEXT_ID),
            self._key(2, default_context_id=self.CONTEXT_ID),
            self._key(3),
        ]
        service = self._service(keys)
        reader = AsyncMock(return_value="alpha")
        with patch("services.credential_default_context.readable_context_name", new=reader):
            out = await service.get_or_create_credentials(
                self.WORKSPACE_ID, "member-1", requester_id="admin-1"
            )

        assert [k["default_context_name"] for k in out["api_keys"]] == ["alpha", "alpha", None]
        assert [k["default_context_id"] for k in out["api_keys"]] == [
            str(self.CONTEXT_ID),
            str(self.CONTEXT_ID),
            None,
        ]
        assert reader.await_count == 1
        kwargs = reader.await_args.kwargs
        assert kwargs["user_id"] == "admin-1"  # the viewer, not the key owner
        assert kwargs["context_id"] == self.CONTEXT_ID
        assert kwargs["key_workspace_id"] == self.WORKSPACE_ID

    @pytest.mark.asyncio
    async def test_unreadable_default_keeps_its_id_without_a_name(self):
        keys = [self._key(1, default_context_id=self.CONTEXT_ID)]
        service = self._service(keys)
        with patch(
            "services.credential_default_context.readable_context_name",
            new=AsyncMock(return_value=None),
        ):
            out = await service.get_or_create_credentials(
                self.WORKSPACE_ID, "member-1", requester_id="member-1"
            )

        (key,) = out["api_keys"]
        assert key["default_context_id"] == str(self.CONTEXT_ID)
        assert key["default_context_name"] is None

    @pytest.mark.asyncio
    async def test_a_failed_name_lookup_keeps_the_key_list(self):
        keys = [self._key(1, default_context_id=self.CONTEXT_ID)]
        service = self._service(keys)
        with patch(
            "services.member_credentials_service.default_context_names",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            out = await service.get_or_create_credentials(
                self.WORKSPACE_ID, "member-1", requester_id="member-1"
            )

        (key,) = out["api_keys"]
        assert key["default_context_id"] == str(self.CONTEXT_ID)
        assert key["default_context_name"] is None


class TestCleanupReownsConnectorKeys:
    """#2352: the removed member's live connector keys in the workspace go to
    its owner before the member's other keys are deleted."""

    @pytest.mark.asyncio
    async def test_reown_runs_before_the_api_keys_delete_and_is_reported(self):
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.sql.dml import Delete, Update

        captured: list[Any] = []

        def _execute(stmt, *a, **k):
            captured.append(stmt)
            result = MagicMock(rowcount=1 if isinstance(stmt, Update) else 2)
            result.scalars.return_value.all.return_value = []
            return result

        db = MagicMock()
        db.execute = AsyncMock(side_effect=_execute)
        workspace_id = uuid4()

        with patch("services.member_credentials_service.logger") as mock_logger:
            out = await MemberCredentialsService(db).cleanup_member_credentials(
                workspace_id, "member-1"
            )

        reown, delete_keys = captured[0], captured[1]
        assert isinstance(reown, Update)
        sql = " ".join(str(reown.compile(dialect=postgresql.dialect())).split())
        assert sql.startswith(
            "UPDATE api_keys SET user_id=workspaces.owner_user_id FROM workspaces WHERE "
        )
        assert "api_keys.workspace_id = %(workspace_id_" in sql
        params = reown.compile().params
        assert params["workspace_id_1"] == workspace_id
        assert params["workspace_id_2"] == workspace_id
        assert isinstance(delete_keys, Delete)
        assert delete_keys.table.name == "api_keys"

        assert out["connector_api_keys_reowned"] == 1
        assert out["api_keys_deleted"] == 2
        (call,) = mock_logger.info.call_args_list
        assert call.args[0] == "member_credentials_cleaned_up"
        assert call.kwargs["connector_api_keys_reowned"] == 1
