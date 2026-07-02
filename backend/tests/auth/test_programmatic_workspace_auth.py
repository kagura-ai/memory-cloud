"""Unit tests for the shared programmatic-workspace authorization helper.

Issue #1164 / #1165: member/invitation and member-credential endpoints accept
API-key principals at workspace-OWNER role, keep session role semantics
unchanged, and reject OAuth Bearer tokens. This helper centralizes the
principal-discrimination rule (key-PRESENCE tests, fail closed) and the
#963 workspace-scoped-key confinement.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from auth.programmatic_workspace_auth import authorize_workspace_management
from auth.workspace_roles import WorkspaceRole
from utils.exceptions import AuthorizationError, NotFoundException

_WS = uuid.uuid4()


def _session_user(user_id: str = "u-session") -> dict:
    # Session principal carries the OIDC 'sub' claim (set at session creation).
    return {"user_id": user_id, "sub": user_id, "email": "s@x", "role": "user"}


def _api_key_user(user_id: str = "u-key", *, workspace_id=None) -> dict:
    # API-key principal always carries 'api_key_workspace_id' (None for global
    # keys) — presence, not truthiness, is the discriminator.
    return {"user_id": user_id, "api_key_workspace_id": workspace_id, "role": "user"}


def _oauth_user(user_id: str = "u-oauth") -> dict:
    return {"user_id": user_id, "oauth_scope": "memory:read memory:write", "role": "user"}


@pytest.fixture
def perm(monkeypatch):
    """Patch PermissionService so no DB is needed; assert which gate ran."""
    from auth import programmatic_workspace_auth as mod

    inst = AsyncMock()
    monkeypatch.setattr(mod, "PermissionService", lambda db: inst)
    return inst


class TestOAuthRejected:
    async def test_oauth_always_403(self, perm):
        with pytest.raises(AuthorizationError):
            await authorize_workspace_management(
                _oauth_user(), _WS, db=None, session_required_role=WorkspaceRole.MEMBER
            )
        # OAuth is rejected before any permission lookup.
        perm.check_workspace_owner.assert_not_called()
        perm.check_workspace_access.assert_not_called()


class TestApiKeyPrincipal:
    async def test_global_key_owner_check(self, perm):
        # Global key (workspace_id None): no confinement, owner-only.
        who = await authorize_workspace_management(
            _api_key_user(workspace_id=None),
            _WS,
            db=None,
            session_required_role=WorkspaceRole.MEMBER,
        )
        assert who == "api_key"
        perm.check_workspace_owner.assert_awaited_once_with("u-key", _WS)
        perm.check_workspace_access.assert_not_called()

    async def test_scoped_key_matching_path_owner_check(self, perm):
        await authorize_workspace_management(
            _api_key_user(workspace_id=_WS),
            _WS,
            db=None,
            session_required_role=WorkspaceRole.MEMBER,
        )
        perm.check_workspace_owner.assert_awaited_once_with("u-key", _WS)

    async def test_scoped_key_foreign_path_uniform_404(self, perm):
        # #963 confinement: scoped key bound to a different workspace → 404,
        # BEFORE the owner lookup (no existence probing).
        other = uuid.uuid4()
        with pytest.raises(NotFoundException):
            await authorize_workspace_management(
                _api_key_user(workspace_id=other),
                _WS,
                db=None,
                session_required_role=WorkspaceRole.MEMBER,
            )
        perm.check_workspace_owner.assert_not_called()

    async def test_api_key_non_owner_denied(self, perm):
        perm.check_workspace_owner.side_effect = AuthorizationError("not owner")
        with pytest.raises(AuthorizationError):
            await authorize_workspace_management(
                _api_key_user(workspace_id=None),
                _WS,
                db=None,
                session_required_role=WorkspaceRole.MEMBER,
            )


class TestSessionPrincipal:
    async def test_session_uses_supplied_role_gate(self, perm):
        who = await authorize_workspace_management(
            _session_user(), _WS, db=None, session_required_role=WorkspaceRole.ADMIN
        )
        assert who == "session"
        # Session keeps its endpoint-specific role gate; owner-only does NOT apply.
        perm.check_workspace_access.assert_awaited_once_with(
            "u-session", _WS, required_role=WorkspaceRole.ADMIN
        )
        perm.check_workspace_owner.assert_not_called()

    async def test_session_denied_propagates(self, perm):
        perm.check_workspace_access.side_effect = AuthorizationError("role too low")
        with pytest.raises(AuthorizationError):
            await authorize_workspace_management(
                _session_user(), _WS, db=None, session_required_role=WorkspaceRole.OWNER
            )


class TestFailClosed:
    async def test_unrecognized_principal_denied(self, perm):
        # No 'sub', no 'oauth_scope', no 'api_key_workspace_id' → fail closed.
        with pytest.raises(AuthorizationError):
            await authorize_workspace_management(
                {"user_id": "u-x", "role": "user"},
                _WS,
                db=None,
                session_required_role=WorkspaceRole.MEMBER,
            )
        perm.check_workspace_owner.assert_not_called()
        perm.check_workspace_access.assert_not_called()
