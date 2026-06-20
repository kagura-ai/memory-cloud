"""Unit tests for the internal billing entitlement-push endpoint (Issue #954).

TestClient + mocked DB. Exercises the real service-token auth (503/401), the
wire-contract validation (400), 404, and the idempotent plan/addon set (200).
DB-level persistence + idempotency is pinned separately in
tests/integration/test_internal_billing_plan_db.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
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


@pytest.fixture
def billing(monkeypatch):
    """Harness for the internal billing endpoint.

    Replaces the per-test ``_settings``/``try-finally``/``_teardown`` boilerplate:
    - ``monkeypatch`` patches the token the auth dependency reads and auto-undoes
      it at teardown (no manual ``patcher.stop()``).
    - ``app.dependency_overrides`` is cleared after every test (no manual
      ``_teardown()``), so a failing test can't leak an override into later ones.

    Usage: ``billing.token = "..."`` sets the configured service token (default
    ``"secret"``); ``billing.client(ws)`` returns a TestClient whose mocked DB
    resolves the workspace lookup to ``ws`` (pass ``None`` for the 404 path).
    """
    mock_settings = MagicMock()
    mock_settings.billing_service_token = "secret"
    monkeypatch.setattr("api.routes.internal_billing.get_settings", lambda: mock_settings)

    class _Harness:
        @property
        def token(self) -> str:
            return mock_settings.billing_service_token

        @token.setter
        def token(self, value: str) -> None:
            mock_settings.billing_service_token = value

        def client(self, workspace=None) -> TestClient:
            async def mock_db():
                db = MagicMock()
                exec_result = MagicMock()
                exec_result.scalar_one_or_none.return_value = workspace
                db.execute = AsyncMock(return_value=exec_result)
                db.commit = AsyncMock()
                yield db

            app.dependency_overrides[get_db] = mock_db
            return TestClient(app, raise_server_exceptions=False)

    yield _Harness()
    app.dependency_overrides.clear()


def test_unset_token_returns_503(billing):
    billing.token = ""
    resp = billing.client(_make_ws()).put(
        _PATH, json={"plan_name": "pro"}, headers={"Authorization": "Bearer x"}
    )
    assert resp.status_code == 503


def test_missing_authorization_returns_401(billing):
    resp = billing.client(_make_ws()).put(_PATH, json={"plan_name": "pro"})
    assert resp.status_code == 401


def test_invalid_token_returns_401(billing):
    resp = billing.client(_make_ws()).put(
        _PATH, json={"plan_name": "pro"}, headers={"Authorization": "Bearer wrong"}
    )
    assert resp.status_code == 401


def test_invalid_plan_returns_422(billing):
    # Canonical ValidationError (VAL-001 / 422), not a raw HTTPException.
    resp = billing.client(_make_ws()).put(
        _PATH, json={"plan_name": "enterprise"}, headers={"Authorization": "Bearer secret"}
    )
    assert resp.status_code == 422


def test_unknown_addon_returns_422(billing):
    resp = billing.client(_make_ws()).put(
        _PATH,
        json={"plan_name": "pro", "addons": {"bogus": 1}},
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 422


def test_negative_addon_returns_422(billing):
    resp = billing.client(_make_ws()).put(
        _PATH,
        json={"plan_name": "pro", "addons": {"memory": -5}},
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 422


def test_workspace_not_found_returns_404(billing):
    resp = billing.client(workspace=None).put(
        _PATH, json={"plan_name": "pro"}, headers={"Authorization": "Bearer secret"}
    )
    assert resp.status_code == 404


def test_sets_plan_and_addons(billing):
    ws = _make_ws()
    resp = billing.client(ws).put(
        _PATH,
        json={
            "plan_name": "pro",
            "status": "active",
            "addons": {"sleep_contexts": 2, "storage_mb": 1024},
        },
        headers={"Authorization": "Bearer secret"},
    )
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
