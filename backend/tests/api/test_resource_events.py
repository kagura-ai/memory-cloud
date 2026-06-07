"""Route-level tests for the resource event data browser (Issue #316).

``GET /api/v1/resources/{resource_id}/events``

Unit-level — owner gate (403), cursor validation (400), response shape, and
the inline payload-size guard, with the service layer mocked via
``dependency_overrides`` + ``patch`` (no DB). The cursor/filter/keyset
semantics against a real database live in
``tests/integration/test_resource_events_query.py``.

Same pattern as ``test_resource_owner_gate.py``: ``require_workspace_owner``
is overridden so the role gate is exercised in isolation.
"""

from datetime import datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import (
    get_user_from_api_key_or_session,
    require_workspace_owner,
)
from db.base import get_db
from models.resource import ResourceEvent

WORKSPACE_ID = uuid4()
EVENTS_PATH = "/api/v1/resources/ec-products/events"


def _mock_user(workspace_role: str) -> dict:
    return {
        "user_id": f"test_{workspace_role}",
        "email": f"{workspace_role}@test.com",
        "role": "user",
        "current_workspace_id": WORKSPACE_ID,
        "workspace_role": workspace_role,
    }


def _make_event(**overrides) -> ResourceEvent:
    """Build an in-memory ResourceEvent (no DB) for shape assertions."""
    defaults = {
        "id": 1,
        "resource_id": "ec-products",
        "op": "upsert",
        "doc_id": "sku-1",
        "version": 1,
        "payload": {"name": "widget", "price": 10},
        "idempotency_key": None,
        "importance": 0.6,
        "created_at": datetime(2026, 6, 7, 12, 0, 0),
        "event_metadata": {},
    }
    defaults.update(overrides)
    return ResourceEvent(**defaults)


async def _mock_db():
    # The service is patched in every test, so the session is never used.
    yield None


@pytest.fixture
def owner_client():
    """Client authenticated as workspace owner; passes the WorkspaceOwner gate."""

    async def mock_auth():
        return _mock_user("owner")

    async def mock_owner():
        return ("test_owner", WORKSPACE_ID)

    app.dependency_overrides[get_user_from_api_key_or_session] = mock_auth
    app.dependency_overrides[require_workspace_owner] = mock_owner
    app.dependency_overrides[get_db] = _mock_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def non_owner_client():
    """Client authenticated as a non-owner — WorkspaceOwner rejects at 403."""

    async def mock_auth():
        return _mock_user("member")

    async def mock_reject_owner():
        raise HTTPException(status_code=403, detail="Requires 'owner' role or higher")

    app.dependency_overrides[get_user_from_api_key_or_session] = mock_auth
    app.dependency_overrides[require_workspace_owner] = mock_reject_owner
    app.dependency_overrides[get_db] = _mock_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


def test_events_requires_owner(non_owner_client):
    """Non-owners are rejected with 403 (parity with #389 resources surface)."""
    resp = non_owner_client.get(EVENTS_PATH)
    assert resp.status_code == 403


def test_events_invalid_cursor_returns_400(owner_client):
    """A non-integer cursor is a client error, not a 500. Cursor validation
    runs before the service call, so no service mock is needed."""
    resp = owner_client.get(EVENTS_PATH, params={"cursor": "not-an-int"})
    assert resp.status_code == 400


def test_events_returns_shape_and_cursor(owner_client):
    """Happy path returns the documented record shape + next_cursor."""
    events = [_make_event(id=5, doc_id="sku-5"), _make_event(id=4, doc_id="sku-4")]
    with patch(
        "api.routes.resources.list_resource_events",
        new=AsyncMock(return_value=(events, "4")),
    ):
        resp = owner_client.get(EVENTS_PATH, params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] == "4"
    assert len(body["events"]) == 2
    first = body["events"][0]
    assert first["id"] == 5
    assert first["op"] == "upsert"
    assert first["doc_id"] == "sku-5"
    assert first["payload"] == {"name": "widget", "price": 10}
    assert first["payload_truncated"] is False
    assert first["payload_bytes"] > 0
    # ISO 8601 UTC with explicit Z suffix (JS-client safe).
    assert first["created_at"].endswith("Z")


def test_events_empty_page_null_cursor(owner_client):
    """An empty result set yields events=[] and next_cursor=null."""
    with patch(
        "api.routes.resources.list_resource_events",
        new=AsyncMock(return_value=([], None)),
    ):
        resp = owner_client.get(EVENTS_PATH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["next_cursor"] is None


def test_events_large_payload_truncated(owner_client):
    """Payloads over the inline cap are omitted with payload_truncated=True."""
    big = _make_event(id=9, payload={"blob": "x" * 1_100_000})
    with patch(
        "api.routes.resources.list_resource_events",
        new=AsyncMock(return_value=([big], None)),
    ):
        resp = owner_client.get(EVENTS_PATH)
    assert resp.status_code == 200
    rec = resp.json()["events"][0]
    assert rec["payload"] is None
    assert rec["payload_truncated"] is True
    assert rec["payload_bytes"] > 1_000_000


def test_events_delete_op_null_payload(owner_client):
    """Delete events have null payload and payload_bytes 0, not truncated."""
    delete_ev = _make_event(id=3, op="delete", version=None, payload=None)
    with patch(
        "api.routes.resources.list_resource_events",
        new=AsyncMock(return_value=([delete_ev], None)),
    ):
        resp = owner_client.get(EVENTS_PATH)
    assert resp.status_code == 200
    rec = resp.json()["events"][0]
    assert rec["op"] == "delete"
    assert rec["payload"] is None
    assert rec["payload_bytes"] == 0
    assert rec["payload_truncated"] is False
    assert rec["version"] is None
