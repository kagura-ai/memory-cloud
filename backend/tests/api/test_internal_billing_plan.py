"""Unit tests for the internal billing entitlement-push endpoint (Issue #954).

TestClient + mocked DB. Exercises the real service-token auth (503/401), the
wire-contract validation (400), 404, and the idempotent plan/addon set (200).
DB-level persistence + idempotency is pinned separately in
tests/integration/test_internal_billing_plan_db.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from api.main import app
from db.base import get_db

_WS_ID = str(uuid4())
_PATH = f"/internal/workspaces/{_WS_ID}/plan"


def _make_ws():
    ws = MagicMock()
    ws.id = _WS_ID
    ws.plan_name = "free"
    # The echo reads ALL addon columns → they must be ints, not MagicMocks.
    for col in (
        "addon_memory_bonus",
        "addon_mcp_quota_bonus",
        "addon_rest_quota_bonus",
        "addon_public_quota_bonus",
        "addon_member_bonus",
        "addon_context_bonus",
        "addon_analysis_bonus",
        "addon_storage_bonus_mb",
        "addon_sleep_contexts_bonus",
        "addon_connector_bonus",
    ):
        setattr(ws, col, 0)
    return ws


def _client(workspace=None):
    async def mock_db():
        db = MagicMock()
        exec_result = MagicMock()
        exec_result.scalar_one_or_none.return_value = workspace
        db.execute = AsyncMock(return_value=exec_result)
        db.commit = AsyncMock()
        yield db

    app.dependency_overrides[get_db] = mock_db
    return TestClient(app, raise_server_exceptions=False)


def _settings(token: str):
    """Patch the token the auth dependency reads."""
    patcher = patch("api.routes.internal_billing.get_settings")
    mock = patcher.start()
    mock.return_value.billing_service_token = token
    return patcher


def _teardown():
    app.dependency_overrides.clear()


def test_unset_token_returns_503():
    p = _settings("")
    client = _client(_make_ws())
    try:
        resp = client.put(_PATH, json={"plan_name": "pro"}, headers={"Authorization": "Bearer x"})
    finally:
        p.stop()
        _teardown()
    assert resp.status_code == 503


def test_missing_authorization_returns_401():
    p = _settings("secret")
    client = _client(_make_ws())
    try:
        resp = client.put(_PATH, json={"plan_name": "pro"})
    finally:
        p.stop()
        _teardown()
    assert resp.status_code == 401


def test_invalid_token_returns_401():
    p = _settings("secret")
    client = _client(_make_ws())
    try:
        resp = client.put(
            _PATH, json={"plan_name": "pro"}, headers={"Authorization": "Bearer wrong"}
        )
    finally:
        p.stop()
        _teardown()
    assert resp.status_code == 401


def test_invalid_plan_returns_400():
    p = _settings("secret")
    client = _client(_make_ws())
    try:
        resp = client.put(
            _PATH,
            json={"plan_name": "enterprise"},
            headers={"Authorization": "Bearer secret"},
        )
    finally:
        p.stop()
        _teardown()
    assert resp.status_code == 400


def test_unknown_addon_returns_400():
    p = _settings("secret")
    client = _client(_make_ws())
    try:
        resp = client.put(
            _PATH,
            json={"plan_name": "pro", "addons": {"bogus": 1}},
            headers={"Authorization": "Bearer secret"},
        )
    finally:
        p.stop()
        _teardown()
    assert resp.status_code == 400


def test_negative_addon_returns_400():
    p = _settings("secret")
    client = _client(_make_ws())
    try:
        resp = client.put(
            _PATH,
            json={"plan_name": "pro", "addons": {"memory": -5}},
            headers={"Authorization": "Bearer secret"},
        )
    finally:
        p.stop()
        _teardown()
    assert resp.status_code == 400


def test_workspace_not_found_returns_404():
    p = _settings("secret")
    client = _client(workspace=None)
    try:
        resp = client.put(
            _PATH, json={"plan_name": "pro"}, headers={"Authorization": "Bearer secret"}
        )
    finally:
        p.stop()
        _teardown()
    assert resp.status_code == 404


def test_sets_plan_and_addons():
    p = _settings("secret")
    ws = _make_ws()
    client = _client(ws)
    try:
        resp = client.put(
            _PATH,
            json={
                "plan_name": "pro",
                "status": "active",
                "addons": {"sleep_contexts": 2, "storage_mb": 1024},
            },
            headers={"Authorization": "Bearer secret"},
        )
    finally:
        p.stop()
        _teardown()
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan_name"] == "pro"
    assert body["addons"]["sleep_contexts"] == 2
    assert body["addons"]["storage_mb"] == 1024
    assert body["status"] == "active"
    assert body["applied"] is True
    # The workspace object was mutated (entitlement is the SoT).
    assert ws.plan_name == "pro"
    assert ws.addon_sleep_contexts_bonus == 2
    assert ws.addon_storage_bonus_mb == 1024
