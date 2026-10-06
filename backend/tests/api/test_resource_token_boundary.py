"""Route-level tests for the resource-token workspace boundary (#268, #1863, #1919).

The boundary is the ``resources`` row a token's ``resource_pk`` points at —
not a live ``contexts`` row and not the token's shadow ``workspace_id``
column — so a token whose contexts were deleted, or whose shadow column was
never backfilled, stays listable, updatable and revocable. Since #1919 the
lookup, the count cap and the quota ceiling share that predicate
(``workspace_tokens``). ``dependency_overrides`` stand in for auth and the
DB; the real-DB walk-throughs live in
``tests/integration/test_resource_tokens_after_context_delete.py`` and
``tests/integration/test_resource_tokens_workspace_scope.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy.dialects import postgresql

import api.routes.resource_tokens as route_module
from api.main import app
from api.routes.resource_tokens import get_resource_token_manager
from auth.dependencies import get_user_from_api_key_or_session, require_workspace_owner
from auth.resource_tokens import (
    ResourceTokenManager,
    workspace_regular_active_tokens,
    workspace_tokens,
)
from db.base import get_db
from models.resource import ResourceToken
from utils.datetime import utcnow

WORKSPACE_ID = uuid4()
USER_ID = "owner_1"
PUBLIC_ID = "rtok_" + "7" * 22

# The attribution clause every lookup, count and sum of the module carries.
_MEMBERSHIP = (
    "resources.workspace_id = %(workspace_id_1)s::UUID "
    "OR resource_tokens.resource_pk IS NULL "
    "AND resource_tokens.workspace_id = %(workspace_id_2)s::UUID"
)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


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


def _no_contexts_query(db: AsyncMock) -> bool:
    """True when none of the executed statements touches the contexts table —
    the property the fix is about, stated without pinning a query count."""
    return all("contexts" not in str(call.args[0]) for call in db.execute.await_args_list)


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


class TestWorkspaceTokensPredicate:
    """#1919: one attribution clause for the lookup, the cap and the ceiling.

    Membership is the ``resources`` row (through ``resource_pk``), with the
    shadow ``workspace_id`` column only for a legacy row that has no
    ``resource_pk`` — the single-token form of ``resource_token_scope``. The
    SQL is pinned here; what it selects on real rows is the integration test.
    """

    def test_lookup_joins_resources_and_never_the_shadow_column_alone(self):
        sql = _sql(workspace_tokens(WORKSPACE_ID, ResourceToken))
        assert "LEFT OUTER JOIN resources ON resources.id = resource_tokens.resource_pk" in sql
        assert f"WHERE {_MEMBERSHIP}" in sql
        assert "contexts" not in sql

    def test_cap_population_is_the_predicate_minus_revoked_and_connector_tokens(self):
        sql = _sql(workspace_regular_active_tokens(WORKSPACE_ID, func.count(ResourceToken.id)))
        assert f"WHERE ({_MEMBERSHIP})" in sql
        assert "resource_tokens.is_active = true" in sql
        assert (
            "LEFT OUTER JOIN workspace_connectors "
            "ON workspace_connectors.resource_pk = resource_tokens.resource_pk" in sql
        )
        assert "workspace_connectors.id IS NULL" in sql
        # The cap is the workspace's, not the caller's (#1919).
        assert "created_by" not in sql


def _lookup_sql(db: AsyncMock) -> str:
    """The first statement the route executed — the token lookup."""
    return _sql(db.execute.await_args_list[0].args[0])


class TestRevokeBoundary:
    def test_revokes_when_the_resources_row_matches_without_any_live_context(self, owner_client):
        pk = uuid4()
        token = _token(resource_pk=pk)
        # One lookup carrying the resources join — and no contexts query
        # anywhere on the path.
        owner_client.state_["db"] = _db_returning(token)
        response = owner_client.delete(f"/api/v1/resource-tokens/{PUBLIC_ID}")
        assert response.status_code == 204, response.text
        owner_client.manager_.revoke_token.assert_awaited_once_with(token.id)
        assert _no_contexts_query(owner_client.state_["db"])
        lookup = _lookup_sql(owner_client.state_["db"])
        assert f"WHERE ({_MEMBERSHIP}) AND resource_tokens.public_id = " in lookup

    def test_404_when_the_token_is_in_another_workspace(self, owner_client):
        # The lookup is scoped to the caller's workspace through the resources
        # row, so a foreign token — or a row whose resource_pk points at
        # another workspace's resource — is a uniform 404 (#268 posture).
        owner_client.state_["db"] = _db_returning(None)
        response = owner_client.delete(f"/api/v1/resource-tokens/{PUBLIC_ID}")
        assert response.status_code == 404
        owner_client.manager_.revoke_token.assert_not_awaited()

    def test_legacy_token_without_resource_pk_is_revocable_in_its_workspace(self, owner_client):
        token = _token(resource_pk=None)
        owner_client.state_["db"] = _db_returning(token)
        response = owner_client.delete(f"/api/v1/resource-tokens/{PUBLIC_ID}")
        assert response.status_code == 204, response.text
        owner_client.manager_.revoke_token.assert_awaited_once_with(token.id)

    def test_revoke_does_not_depend_on_who_minted_the_token(self, owner_client):
        # #1863 "Done when": the owner can revoke every active token in the
        # workspace, including one minted by a departed member or a connector.
        pk = uuid4()
        token = _token(resource_pk=pk)
        token.created_by = "someone_else"
        owner_client.state_["db"] = _db_returning(token)
        response = owner_client.delete(f"/api/v1/resource-tokens/{PUBLIC_ID}")
        assert response.status_code == 204, response.text


class TestUpdateBoundary:
    def test_404_when_the_token_is_in_another_workspace(self, owner_client):
        owner_client.state_["db"] = _db_returning(None)
        response = owner_client.patch(
            f"/api/v1/resource-tokens/{PUBLIC_ID}", json={"description": "renamed"}
        )
        assert response.status_code == 404

    def test_lookup_is_the_ceilings_predicate(self, owner_client):
        # Same boundary as revoke: one lookup through the resources row, no
        # contexts query, no shadow-column-only match (#1919: a token the
        # ceiling counts must be addressable). The quota path continues into
        # plan lookups that the integration test exercises on a real database.
        token = _token(resource_pk=uuid4())
        token.created_at = utcnow()
        owner_client.state_["db"] = _db_returning(token)
        response = owner_client.patch(
            f"/api/v1/resource-tokens/{PUBLIC_ID}", json={"description": "renamed"}
        )
        assert response.status_code == 200, response.text
        assert _no_contexts_query(owner_client.state_["db"])
        lookup = _lookup_sql(owner_client.state_["db"])
        assert f"WHERE ({_MEMBERSHIP}) AND resource_tokens.public_id = " in lookup
        assert owner_client.state_["db"].execute.await_count == 1


class TestListFilterBoundary:
    def test_filter_resolves_through_the_resources_row_and_pins_the_pk(self, owner_client):
        pk = uuid4()
        with patch.object(
            route_module, "resolve_resource_pk", new=AsyncMock(return_value=pk)
        ) as resolve:
            response = owner_client.get("/api/v1/resource-tokens", params={"resource_id": "orders"})
        assert response.status_code == 200, response.text
        resolve.assert_awaited_once_with(owner_client.state_["db"], WORKSPACE_ID, "orders")
        # The slug is shared across workspaces; the list is pinned to the
        # resolved resources row and the caller's workspace, never to the slug.
        owner_client.manager_.list_tokens.assert_awaited_once_with(
            include_revoked=True, limit=50, offset=0, workspace_id=WORKSPACE_ID, resource_pk=pk
        )
        owner_client.manager_.count_tokens.assert_awaited_once_with(
            include_revoked=True, workspace_id=WORKSPACE_ID, resource_pk=pk
        )

    def test_unfiltered_list_is_the_whole_workspace(self, owner_client):
        response = owner_client.get("/api/v1/resource-tokens")
        assert response.status_code == 200, response.text
        owner_client.manager_.list_tokens.assert_awaited_once_with(
            include_revoked=True, limit=50, offset=0, workspace_id=WORKSPACE_ID, resource_pk=None
        )

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


class TestSlugFilterNeedsScope:
    """#1877: a slug is shared across workspaces, so the manager refuses to
    list or count by it unless a workspace or a ``resources`` row pins it —
    the slug-only call that revoked another workspace's tokens cannot come
    back unnoticed."""

    @pytest.mark.asyncio
    async def test_list_by_bare_slug_is_refused(self):
        db = AsyncMock()
        with pytest.raises(ValueError, match="workspace_id or resource_pk"):
            await ResourceTokenManager(db).list_tokens(resource_id="orders")
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_count_by_bare_slug_is_refused(self):
        db = AsyncMock()
        with pytest.raises(ValueError, match="workspace_id or resource_pk"):
            await ResourceTokenManager(db).count_tokens(resource_id="orders")
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scope", [{"workspace_id": WORKSPACE_ID}, {"resource_pk": uuid4()}])
    async def test_slug_with_a_scope_is_accepted(self, scope):
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        result.scalar.return_value = 0
        db = AsyncMock(execute=AsyncMock(return_value=result))
        manager = ResourceTokenManager(db)
        assert await manager.list_tokens(resource_id="orders", **scope) == []
        assert await manager.count_tokens(resource_id="orders", **scope) == 0

    @pytest.mark.asyncio
    async def test_unfiltered_list_is_still_allowed(self):
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db = AsyncMock(execute=AsyncMock(return_value=result))
        assert await ResourceTokenManager(db).list_tokens() == []
