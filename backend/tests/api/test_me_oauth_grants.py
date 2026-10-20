"""The connected-apps routes (#2129): list grants, set / clear a default context.

``GET /api/v1/me/oauth/grants``, ``PUT`` and ``DELETE
/api/v1/me/oauth/grants/{client_id}/default-context``. The service layer is
patched; the routes are called directly. The DB-backed behaviour of the
service is pinned in ``tests/services/test_credential_default_context_db.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routes.me_oauth_grants import (
    OAuthGrantDefaultContextUpdate,
    clear_oauth_grant_default_context,
    list_oauth_grants,
    set_oauth_grant_default_context,
)
from services.credential_default_context import GrantSummary
from utils.datetime import utcnow
from utils.exceptions import MemoryCloudException

USER = {"user_id": "user-1", "email": "u@example.com", "role": "user"}
KEY_USER = {**USER, "api_key_workspace_id": uuid.uuid4()}
CONTEXT_ID = uuid.uuid4()
MODULE = "api.routes.me_oauth_grants"


def _grant(**over) -> GrantSummary:
    base = {
        "client_id": "client-1",
        "client_name": "Desktop App",
        "scopes": ["memory:read", "memory:write"],
        "default_context_id": None,
        "last_issued_at": utcnow(),
    }
    base.update(over)
    return GrantSummary(**base)


def _db() -> MagicMock:
    return MagicMock(commit=AsyncMock())


@pytest.mark.asyncio
async def test_list_returns_the_grants_with_readable_names():
    grants = [_grant(default_context_id=CONTEXT_ID), _grant(client_id="client-2")]
    names = AsyncMock(return_value="proj-alpha")
    with (
        patch(f"{MODULE}.list_grants", new=AsyncMock(return_value=grants)) as listed,
        patch(f"{MODULE}.readable_context_name", new=names),
    ):
        response = await list_oauth_grants(user=KEY_USER, db=_db())

    assert listed.await_args.kwargs == {"user_id": "user-1"}
    assert [r.client_id for r in response] == ["client-1", "client-2"]
    assert response[0].default_context_name == "proj-alpha"
    assert response[1].default_context_name is None
    assert response[0].scopes == ["memory:read", "memory:write"]
    # Named in the caller's key scope; only the grant with a default is read.
    names.assert_awaited_once()
    assert names.await_args.kwargs["key_workspace_id"] == KEY_USER["api_key_workspace_id"]


@pytest.mark.asyncio
async def test_put_sets_a_readable_default():
    db = _db()
    setter = AsyncMock()
    with (
        patch(f"{MODULE}.list_grants", new=AsyncMock(return_value=[_grant()])),
        patch(
            f"{MODULE}.readable_context",
            new=AsyncMock(return_value=SimpleNamespace(name="proj-alpha")),
        ),
        patch(f"{MODULE}.set_grant_default_context", new=setter),
    ):
        response = await set_oauth_grant_default_context(
            client_id="client-1",
            data=OAuthGrantDefaultContextUpdate(default_context_id=CONTEXT_ID),
            user=USER,
            db=db,
        )

    setter.assert_awaited_once_with(
        db, user_id="user-1", client_id="client-1", context_id=CONTEXT_ID
    )
    db.commit.assert_awaited_once()
    assert response.default_context_id == CONTEXT_ID
    assert response.default_context_name == "proj-alpha"


@pytest.mark.asyncio
async def test_put_with_an_unreadable_context_is_a_uniform_404():
    db = _db()
    setter = AsyncMock()
    with (
        patch(f"{MODULE}.list_grants", new=AsyncMock(return_value=[_grant()])),
        patch(f"{MODULE}.readable_context", new=AsyncMock(return_value=None)),
        patch(f"{MODULE}.set_grant_default_context", new=setter),
        pytest.raises(MemoryCloudException) as exc,
    ):
        await set_oauth_grant_default_context(
            client_id="client-1",
            data=OAuthGrantDefaultContextUpdate(default_context_id=CONTEXT_ID),
            user=USER,
            db=db,
        )

    assert exc.value.status_code == 404
    assert exc.value.message == "Context not found"
    setter.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["put", "delete"])
async def test_a_grant_the_caller_does_not_hold_is_404(method):
    db = _db()
    setter = AsyncMock()
    with (
        patch(f"{MODULE}.list_grants", new=AsyncMock(return_value=[_grant()])),
        patch(f"{MODULE}.set_grant_default_context", new=setter),
        pytest.raises(MemoryCloudException) as exc,
    ):
        if method == "put":
            await set_oauth_grant_default_context(
                client_id="someone-elses",
                data=OAuthGrantDefaultContextUpdate(default_context_id=None),
                user=USER,
                db=db,
            )
        else:
            await clear_oauth_grant_default_context(client_id="someone-elses", user=USER, db=db)

    assert exc.value.status_code == 404
    assert exc.value.message == "OAuth grant not found"
    setter.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_null_and_delete_clear_without_a_read():
    for call in ("put", "delete"):
        db = _db()
        setter = AsyncMock()
        reader = AsyncMock()
        grants = [_grant(default_context_id=CONTEXT_ID)]
        with (
            patch(f"{MODULE}.list_grants", new=AsyncMock(return_value=grants)),
            patch(f"{MODULE}.readable_context", new=reader),
            patch(f"{MODULE}.readable_context_name", new=reader),
            patch(f"{MODULE}.set_grant_default_context", new=setter),
        ):
            if call == "put":
                response = await set_oauth_grant_default_context(
                    client_id="client-1",
                    data=OAuthGrantDefaultContextUpdate(default_context_id=None),
                    user=USER,
                    db=db,
                )
                assert response.default_context_id is None
            else:
                assert (
                    await clear_oauth_grant_default_context(client_id="client-1", user=USER, db=db)
                    is None
                )

        setter.assert_awaited_once_with(db, user_id="user-1", client_id="client-1", context_id=None)
        reader.assert_not_awaited()
        db.commit.assert_awaited_once()


def test_the_routes_are_registered_under_api_v1_me_oauth():
    from tests.api.test_capacity_lock_route_table import _registered_routes

    routes = _registered_routes()
    assert routes[("GET", "/api/v1/me/oauth/grants")] == "me_oauth_grants"
    assert routes[("PUT", "/api/v1/me/oauth/grants/{client_id}/default-context")]
    assert routes[("DELETE", "/api/v1/me/oauth/grants/{client_id}/default-context")]
    assert routes[("PATCH", "/api/v1/config/api-keys/{key_id}")] == "api_keys"
