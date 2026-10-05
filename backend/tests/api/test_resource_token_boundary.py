"""Route-level tests for the resource-token workspace boundary (#268, #1863).

The boundary is the ``resources`` row a token's ``resource_pk`` points at —
not a live ``contexts`` row — so a token whose contexts were deleted stays
listable, updatable and revocable. ``dependency_overrides`` stand in for
auth and the DB; the real-DB walk-through lives in
``tests/integration/test_resource_tokens_after_context_delete.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import api.routes.resource_tokens as route_module
from api.main import app
from api.routes.resource_tokens import _token_in_workspace, get_resource_token_manager
from auth.dependencies import get_user_from_api_key_or_session, require_workspace_owner
from db.base import get_db
from models.resource import ResourceToken

WORKSPACE_ID = uuid4()
USER_ID = "owner_1"
PUBLIC_ID = "rtok_" + "7" * 22


def _token(resource_pk=None) -> ResourceToken:
    return ResourceToken(
        id=7,
        public_id=PUBLIC_ID,
        resource_pk=resource_pk,
        resource_id="orders",
        workspace_id=WORKSPACE_ID,
        token_hash="h" * 64,
        quota_events_per_hour=1000,
        created_by=USER_ID,
        is_active=True,
    )


def _db_returning(*scalars) -> AsyncMock:
    """A session whose successive ``execute`` calls yield the given scalars."""
    results = []
    for value in scalars:
        result = MagicMock()
        result.scalar_one_or_none.return_value = value
        results.append(result)
    return AsyncMock(execute=AsyncMock(side_effect=results))


@pytest.fixture
def owner_client():
    manager = MagicMock()
    manager.revoke_token = AsyncMock()
    manager.count_tokens = AsyncMock(return_value=0)
    manager.list_tokens = AsyncMock(return_value=[])
    state: dict = {"db": AsyncMock()}

    async def mock_auth():
        return {
            "user_id": USER_ID,
            "email": "owner@test.com",
            "role": "user",
            "current_workspace_id": WORKSPACE_ID,
            "workspace_role": "owner",
        }

    async def mock_owner():
        return (USER_ID, WORKSPACE_ID)

    async def mock_db():
        yield state["db"]

    async def mock_manager():
        return manager

    app.dependency_overrides[get_user_from_api_key_or_session] = mock_auth
    app.dependency_overrides[require_workspace_owner] = mock_owner
    app.dependency_overrides[get_db] = mock_db
    app.dependency_overrides[get_resource_token_manager] = mock_manager
    with TestClient(app, raise_server_exceptions=False) as client:
        client.state_ = state  # type: ignore[attr-defined]
        client.manager_ = manager  # type: ignore[attr-defined]
        yield client
    app.dependency_overrides.clear()


class TestTokenInWorkspace:
    @pytest.mark.asyncio
    async def test_true_when_the_resources_row_is_in_the_workspace(self):
        pk = uuid4()
        db = _db_returning(pk)
        assert await _token_in_workspace(db, _token(resource_pk=pk), WORKSPACE_ID) is True

    @pytest.mark.asyncio
    async def test_false_when_no_resources_row_matches(self):
        db = _db_returning(None)
        assert await _token_in_workspace(db, _token(resource_pk=uuid4()), WORKSPACE_ID) is False

    @pytest.mark.asyncio
    async def test_legacy_token_without_resource_pk_is_out_of_scope(self):
        db = AsyncMock()
        assert await _token_in_workspace(db, _token(resource_pk=None), WORKSPACE_ID) is False
        db.execute.assert_not_awaited()


class TestRevokeBoundary:
    def test_revokes_when_the_resources_row_matches_without_any_live_context(self, owner_client):
        pk = uuid4()
        token = _token(resource_pk=pk)
        # 1st execute: token lookup; 2nd: resources row. No contexts query at all.
        owner_client.state_["db"] = _db_returning(token, pk)
        response = owner_client.delete(f"/api/v1/resource-tokens/{PUBLIC_ID}")
        assert response.status_code == 204, response.text
        owner_client.manager_.revoke_token.assert_awaited_once_with(token.id)
        assert owner_client.state_["db"].execute.await_count == 2

    def test_403_when_the_resources_row_is_in_another_workspace(self, owner_client):
        owner_client.state_["db"] = _db_returning(_token(resource_pk=uuid4()), None)
        response = owner_client.delete(f"/api/v1/resource-tokens/{PUBLIC_ID}")
        assert response.status_code == 403
        owner_client.manager_.revoke_token.assert_not_awaited()

    def test_403_for_a_legacy_token_without_resource_pk(self, owner_client):
        owner_client.state_["db"] = _db_returning(_token(resource_pk=None))
        response = owner_client.delete(f"/api/v1/resource-tokens/{PUBLIC_ID}")
        assert response.status_code == 403

    def test_404_when_the_token_is_not_the_callers(self, owner_client):
        owner_client.state_["db"] = _db_returning(None)
        response = owner_client.delete(f"/api/v1/resource-tokens/{PUBLIC_ID}")
        assert response.status_code == 404


class TestUpdateBoundary:
    def test_403_when_the_resources_row_is_in_another_workspace(self, owner_client):
        # Same boundary as revoke: token lookup, then the resources row — no
        # contexts query. (The success path continues into plan/quota lookups
        # that the integration test exercises against a real database.)
        owner_client.state_["db"] = _db_returning(_token(resource_pk=uuid4()), None)
        response = owner_client.patch(
            f"/api/v1/resource-tokens/{PUBLIC_ID}", json={"description": "renamed"}
        )
        assert response.status_code == 403
        assert owner_client.state_["db"].execute.await_count == 2

    def test_403_for_a_legacy_token_without_resource_pk(self, owner_client):
        owner_client.state_["db"] = _db_returning(_token(resource_pk=None))
        response = owner_client.patch(
            f"/api/v1/resource-tokens/{PUBLIC_ID}", json={"description": "renamed"}
        )
        assert response.status_code == 403


class TestListFilterBoundary:
    def test_filter_resolves_through_the_resources_row(self, owner_client):
        with patch.object(
            route_module, "resolve_resource_pk", new=AsyncMock(return_value=uuid4())
        ) as resolve:
            response = owner_client.get("/api/v1/resource-tokens", params={"resource_id": "orders"})
        assert response.status_code == 200, response.text
        resolve.assert_awaited_once_with(owner_client.state_["db"], WORKSPACE_ID, "orders")

    def test_403_when_the_slug_has_no_resources_row_in_the_workspace(self, owner_client):
        with patch.object(route_module, "resolve_resource_pk", new=AsyncMock(return_value=None)):
            response = owner_client.get("/api/v1/resource-tokens", params={"resource_id": "orders"})
        assert response.status_code == 403


class TestCreateWithoutLiveContext:
    """Minting still needs a live context (#268); the message says so when the
    resource itself exists (#1863) instead of claiming it is not found."""

    def _post(self, owner_client, db):
        owner_client.state_["db"] = db
        return owner_client.post(
            "/api/v1/resource-tokens",
            json={"resource_id": "orders", "description": "x", "quota_events_per_hour": 100},
        )

    def test_names_the_missing_context_when_the_resource_exists(self, owner_client):
        with patch.object(route_module, "resolve_resource_pk", new=AsyncMock(return_value=uuid4())):
            response = self._post(owner_client, _db_returning(None))
        assert response.status_code == 403
        assert "no live context" in response.json()["message"]

    def test_keeps_the_uniform_message_when_the_resource_is_unknown(self, owner_client):
        with patch.object(route_module, "resolve_resource_pk", new=AsyncMock(return_value=None)):
            response = self._post(owner_client, _db_returning(None))
        assert response.status_code == 403
        assert "not found in your workspace" in response.json()["message"]
