"""RBAC tests for ContextMember endpoints (Issue #362).

Covers the three new backend guards that UI gating cannot protect:

1. Self-removal guard — cannot remove yourself from a context
2. Last-owner demotion guard — cannot demote the sole ContextMember owner
3. Workspace-external user_id guard — cannot add a non-workspace user

Plus the permission-check contract for the four ContextMember routes:
list / add / update role / remove.

Uses unittest.mock.patch on ``api.routes.contexts.get_current_user`` and
``PermissionService`` methods — no DB or Docker required, matching the
``test_rbac_issue59.py`` / ``test_workspace_isolation_issue50.py`` style.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.main import app

WORKSPACE_ID = uuid4()
CONTEXT_ID = uuid4()

OWNER_USER_ID = "owner_user"
ADMIN_USER_ID = "admin_user"
MEMBER_USER_ID = "member_user"
VIEWER_USER_ID = "viewer_user"
OTHER_USER_ID = "other_user"


# ============================================================================
# Helpers
# ============================================================================


def _mock_user(user_id: str) -> dict:
    return {
        "user_id": user_id,
        "email": f"{user_id}@test.com",
        "role": "user",
        "current_workspace_id": WORKSPACE_ID,
    }


def _mock_context(is_private: bool = False):
    """Build a mock Context row suitable for check_context_owner returns."""
    ctx = MagicMock()
    ctx.id = CONTEXT_ID
    ctx.workspace_id = WORKSPACE_ID
    ctx.is_private = is_private
    ctx.created_by = OWNER_USER_ID
    return ctx


def _owner_allows():
    """PermissionService.check_context_owner stub that permits the caller."""
    return AsyncMock(return_value=_mock_context())


def _owner_denies():
    """PermissionService.check_context_owner stub that raises 403."""

    async def _raise(*args, **kwargs):
        raise HTTPException(status_code=403, detail="Not context owner")

    return _raise


@pytest.fixture
def client():
    """TestClient without session middleware overrides — we patch get_current_user."""
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ============================================================================
# Guard 1 — Self-removal (DELETE /contexts/{id}/members/{user_id})
# ============================================================================


class TestSelfRemovalGuard:
    def test_owner_cannot_remove_self(self, client):
        with (
            patch(
                "api.routes.contexts.get_current_user",
                AsyncMock(return_value=_mock_user(OWNER_USER_ID)),
            ),
            patch(
                "services.permission_service.PermissionService.check_context_owner",
                _owner_allows(),
            ),
        ):
            response = client.delete(f"/api/v1/contexts/{CONTEXT_ID}/members/{OWNER_USER_ID}")

        assert response.status_code == 400
        assert "yourself" in response.json()["detail"].lower()

    def test_workspace_admin_cannot_remove_self(self, client):
        """check_context_owner promotes workspace admin to owner — guard must still fire."""
        with (
            patch(
                "api.routes.contexts.get_current_user",
                AsyncMock(return_value=_mock_user(ADMIN_USER_ID)),
            ),
            patch(
                "services.permission_service.PermissionService.check_context_owner",
                _owner_allows(),
            ),
        ):
            response = client.delete(f"/api/v1/contexts/{CONTEXT_ID}/members/{ADMIN_USER_ID}")

        assert response.status_code == 400
        assert "yourself" in response.json()["detail"].lower()

    def test_owner_can_remove_another_user_reaches_member_lookup(self, client):
        """Removing someone else passes the self-removal guard (goes on to member lookup)."""
        # Member lookup returns None → 404 (expected, not 400 "yourself")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        fake_session = MagicMock()
        fake_session.execute = AsyncMock(return_value=mock_result)

        async def override_db():
            yield fake_session

        from db.base import get_db

        app.dependency_overrides[get_db] = override_db
        try:
            with (
                patch(
                    "api.routes.contexts.get_current_user",
                    AsyncMock(return_value=_mock_user(OWNER_USER_ID)),
                ),
                patch(
                    "services.permission_service.PermissionService.check_context_owner",
                    _owner_allows(),
                ),
            ):
                response = client.delete(f"/api/v1/contexts/{CONTEXT_ID}/members/{OTHER_USER_ID}")
            assert response.status_code == 404  # member not found, not 400-yourself
        finally:
            app.dependency_overrides.clear()


# ============================================================================
# Guard 2 — Last-owner demotion (PUT /contexts/{id}/members/{user_id})
# ============================================================================


class TestLastOwnerDemotionGuard:
    def _setup_member_lookup(self, member_role: str):
        """Return a db fixture whose member lookup yields a ContextMember with given role."""
        member = MagicMock(role=member_role)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = member

        fake_session = MagicMock()
        fake_session.execute = AsyncMock(return_value=mock_result)
        fake_session.commit = AsyncMock()
        fake_session.refresh = AsyncMock()
        return fake_session, member

    def test_demoting_sole_owner_returns_400(self, client):
        fake_session, _member = self._setup_member_lookup("owner")

        async def override_db():
            yield fake_session

        from db.base import get_db

        app.dependency_overrides[get_db] = override_db
        try:
            with (
                patch(
                    "api.routes.contexts.get_current_user",
                    AsyncMock(return_value=_mock_user(OWNER_USER_ID)),
                ),
                patch(
                    "services.permission_service.PermissionService.check_context_owner",
                    _owner_allows(),
                ),
                patch(
                    "services.permission_service.PermissionService.count_context_owners",
                    AsyncMock(return_value=1),
                ),
            ):
                response = client.put(
                    f"/api/v1/contexts/{CONTEXT_ID}/members/{OTHER_USER_ID}",
                    json={"role": "editor"},
                )
            assert response.status_code == 400
            assert "last" in response.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()

    def test_demoting_owner_when_another_owner_exists_is_allowed(self, client):
        fake_session, _member = self._setup_member_lookup("owner")

        async def override_db():
            yield fake_session

        from db.base import get_db

        app.dependency_overrides[get_db] = override_db
        try:
            with (
                patch(
                    "api.routes.contexts.get_current_user",
                    AsyncMock(return_value=_mock_user(OWNER_USER_ID)),
                ),
                patch(
                    "services.permission_service.PermissionService.check_context_owner",
                    _owner_allows(),
                ),
                patch(
                    "services.permission_service.PermissionService.count_context_owners",
                    AsyncMock(return_value=2),
                ),
            ):
                response = client.put(
                    f"/api/v1/contexts/{CONTEXT_ID}/members/{OTHER_USER_ID}",
                    json={"role": "editor"},
                )
            # Not 400 from guard — may be 200/500 depending on downstream but NEVER 400-last-owner
            assert response.status_code != 400, (
                f"Guard fired incorrectly when 2 owners exist: {response.json()}"
            )
        finally:
            app.dependency_overrides.clear()

    def test_promoting_editor_to_owner_skips_guard(self, client):
        """Editor → owner has no last-owner risk; guard must not call count_context_owners."""
        fake_session, _member = self._setup_member_lookup("editor")

        async def override_db():
            yield fake_session

        from db.base import get_db

        app.dependency_overrides[get_db] = override_db
        count_spy = AsyncMock(return_value=1)
        try:
            with (
                patch(
                    "api.routes.contexts.get_current_user",
                    AsyncMock(return_value=_mock_user(OWNER_USER_ID)),
                ),
                patch(
                    "services.permission_service.PermissionService.check_context_owner",
                    _owner_allows(),
                ),
                patch(
                    "services.permission_service.PermissionService.count_context_owners",
                    count_spy,
                ),
            ):
                response = client.put(
                    f"/api/v1/contexts/{CONTEXT_ID}/members/{OTHER_USER_ID}",
                    json={"role": "owner"},
                )
            count_spy.assert_not_called()
            assert response.status_code != 400
        finally:
            app.dependency_overrides.clear()


# ============================================================================
# Guard 3 — Workspace-external user_id on add (POST /contexts/{id}/members)
# ============================================================================


class TestWorkspaceExternalUserGuard:
    def test_adding_non_workspace_member_returns_400(self, client):
        fake_session = MagicMock()
        fake_session.execute = AsyncMock()
        fake_session.commit = AsyncMock()
        fake_session.refresh = AsyncMock()

        async def override_db():
            yield fake_session

        from db.base import get_db

        app.dependency_overrides[get_db] = override_db
        try:
            with (
                patch(
                    "api.routes.contexts.get_current_user",
                    AsyncMock(return_value=_mock_user(OWNER_USER_ID)),
                ),
                patch(
                    "services.permission_service.PermissionService.check_context_owner",
                    _owner_allows(),
                ),
                patch(
                    "services.permission_service.PermissionService.is_workspace_member",
                    AsyncMock(return_value=False),
                ),
            ):
                response = client.post(
                    f"/api/v1/contexts/{CONTEXT_ID}/members",
                    json={"user_id": "outsider", "role": "editor"},
                )
            assert response.status_code == 400
            assert "workspace" in response.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()

    def test_private_context_guard_still_fires_first(self, client):
        """Private-context rejection predates workspace-member check; keep that order."""
        fake_session = MagicMock()
        fake_session.execute = AsyncMock()

        async def override_db():
            yield fake_session

        from db.base import get_db

        app.dependency_overrides[get_db] = override_db

        private_ctx = _mock_context(is_private=True)
        try:
            with (
                patch(
                    "api.routes.contexts.get_current_user",
                    AsyncMock(return_value=_mock_user(OWNER_USER_ID)),
                ),
                patch(
                    "services.permission_service.PermissionService.check_context_owner",
                    AsyncMock(return_value=private_ctx),
                ),
            ):
                response = client.post(
                    f"/api/v1/contexts/{CONTEXT_ID}/members",
                    json={"user_id": "anyone", "role": "editor"},
                )
            assert response.status_code == 400
            assert "private" in response.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()


# ============================================================================
# Permission contract — non-owner caller rejected from mutation endpoints
# ============================================================================


MUTATION_ENDPOINTS = [
    ("POST", f"/api/v1/contexts/{CONTEXT_ID}/members", {"user_id": "x", "role": "editor"}),
    ("PUT", f"/api/v1/contexts/{CONTEXT_ID}/members/{OTHER_USER_ID}", {"role": "editor"}),
    ("DELETE", f"/api/v1/contexts/{CONTEXT_ID}/members/{OTHER_USER_ID}", None),
]


class TestMutationEndpointsRequireContextOwner:
    @pytest.mark.parametrize("method,path,body", MUTATION_ENDPOINTS)
    def test_non_owner_gets_403(self, client, method, path, body):
        with (
            patch(
                "api.routes.contexts.get_current_user",
                AsyncMock(return_value=_mock_user(VIEWER_USER_ID)),
            ),
            patch(
                "services.permission_service.PermissionService.check_context_owner",
                _owner_denies(),
            ),
        ):
            response = client.request(method, path, json=body)
        assert response.status_code == 403, (
            f"{method} {path} returned {response.status_code}, expected 403"
        )


class TestListRequiresViewerAccess:
    def test_non_member_gets_403(self, client):
        async def _raise(*args, **kwargs):
            raise HTTPException(status_code=403, detail="Not a member")

        with (
            patch(
                "api.routes.contexts.get_current_user",
                AsyncMock(return_value=_mock_user(OTHER_USER_ID)),
            ),
            patch(
                "services.permission_service.PermissionService.check_context_access",
                _raise,
            ),
        ):
            response = client.get(f"/api/v1/contexts/{CONTEXT_ID}/members")
        assert response.status_code == 403


# ============================================================================
# Happy-path coverage — owner/admin pass checks, existing guards still enforced
# ============================================================================


class TestAddMemberHappyPath:
    def test_duplicate_member_returns_400(self, client):
        """Existing duplicate-member guard remains functional alongside the new guards."""
        existing = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing

        fake_session = MagicMock()
        fake_session.execute = AsyncMock(return_value=mock_result)

        async def override_db():
            yield fake_session

        from db.base import get_db

        app.dependency_overrides[get_db] = override_db
        try:
            with (
                patch(
                    "api.routes.contexts.get_current_user",
                    AsyncMock(return_value=_mock_user(OWNER_USER_ID)),
                ),
                patch(
                    "services.permission_service.PermissionService.check_context_owner",
                    _owner_allows(),
                ),
                patch(
                    "services.permission_service.PermissionService.is_workspace_member",
                    AsyncMock(return_value=True),
                ),
            ):
                response = client.post(
                    f"/api/v1/contexts/{CONTEXT_ID}/members",
                    json={"user_id": MEMBER_USER_ID, "role": "editor"},
                )
            assert response.status_code == 400
            assert "already" in response.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()


class TestRemoveMemberHappyPath:
    def test_cannot_remove_context_owner_role(self, client):
        """Existing role-is-owner guard remains functional."""
        owner_member = MagicMock(role="owner")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = owner_member

        fake_session = MagicMock()
        fake_session.execute = AsyncMock(return_value=mock_result)

        async def override_db():
            yield fake_session

        from db.base import get_db

        app.dependency_overrides[get_db] = override_db
        try:
            with (
                patch(
                    "api.routes.contexts.get_current_user",
                    AsyncMock(return_value=_mock_user(ADMIN_USER_ID)),
                ),
                patch(
                    "services.permission_service.PermissionService.check_context_owner",
                    _owner_allows(),
                ),
            ):
                response = client.delete(f"/api/v1/contexts/{CONTEXT_ID}/members/{OTHER_USER_ID}")
            assert response.status_code == 400
            assert "owner" in response.json()["detail"].lower()
        finally:
            app.dependency_overrides.clear()


class TestUpdateMemberHappyPath:
    def test_missing_member_returns_404(self, client):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # member not found

        fake_session = MagicMock()
        fake_session.execute = AsyncMock(return_value=mock_result)

        async def override_db():
            yield fake_session

        from db.base import get_db

        app.dependency_overrides[get_db] = override_db
        try:
            with (
                patch(
                    "api.routes.contexts.get_current_user",
                    AsyncMock(return_value=_mock_user(OWNER_USER_ID)),
                ),
                patch(
                    "services.permission_service.PermissionService.check_context_owner",
                    _owner_allows(),
                ),
            ):
                response = client.put(
                    f"/api/v1/contexts/{CONTEXT_ID}/members/{OTHER_USER_ID}",
                    json={"role": "viewer"},
                )
            assert response.status_code == 404
        finally:
            app.dependency_overrides.clear()
