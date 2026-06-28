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
    # #1095: the protective default; a push flips it to external_billing. Set it
    # to a real str (not a MagicMock) so the result/GET echo is JSON-serializable.
    ws.entitlement_source = "admin_grant"
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


def test_oversized_addon_returns_422(billing):
    # Above the INTEGER-column ceiling → clean 422, not a 500 at commit.
    resp = billing.client(_make_ws()).put(
        _PATH,
        json={"plan_name": "pro", "addons": {"memory": 9_999_999_999}},
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
    # #1095: a billing push marks provenance billing-owned (and echoes it).
    assert body["entitlement_source"] == "external_billing"
    # The workspace object was mutated (entitlement is the SoT).
    assert ws.plan_name == "pro"
    assert ws.addon_sleep_contexts_bonus == 2
    assert ws.addon_storage_bonus_mb == 1024
    assert ws.entitlement_source == "external_billing"


def test_addons_full_replace_zeroes_omitted(billing):
    # Over-grant guard: a workspace carrying a prior tier's sleep addon, then a
    # push that lists only `member`, must end with sleep zeroed (not stranded).
    ws = _make_ws()
    ws.addon_sleep_contexts_bonus = 5  # left over from a higher tier
    resp = billing.client(ws).put(
        _PATH,
        json={"plan_name": "basic", "addons": {"member": 2}},
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["addons"]["member"] == 2
    assert body["addons"]["sleep_contexts"] == 0  # omitted → reset, not stranded
    assert ws.addon_member_bonus == 2
    assert ws.addon_sleep_contexts_bonus == 0


def test_empty_addons_object_zeroes_all(billing):
    # An explicit empty object means "no addons" — every dimension resets to 0.
    ws = _make_ws()
    ws.addon_member_bonus = 3
    ws.addon_storage_bonus_mb = 512
    resp = billing.client(ws).put(
        _PATH,
        json={"plan_name": "free", "addons": {}},
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    assert all(v == 0 for v in resp.json()["addons"].values())
    assert ws.addon_member_bonus == 0
    assert ws.addon_storage_bonus_mb == 0


def test_omitted_addons_leaves_existing_unchanged(billing):
    # A tier-only push (no `addons` field) must NOT touch existing addon bonuses.
    ws = _make_ws()
    ws.addon_member_bonus = 3
    resp = billing.client(ws).put(
        _PATH,
        json={"plan_name": "pro"},
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["addons"]["member"] == 3
    assert ws.addon_member_bonus == 3


def test_get_entitlement_returns_source(billing):
    # #1095 reconciler read surface: GET echoes plan + addons + provenance.
    # Use a non-default value (external_billing) so this proves the handler READS
    # workspace.entitlement_source rather than echoing a hardcoded literal.
    ws = _make_ws()
    ws.entitlement_source = "external_billing"
    resp = billing.client(ws).get(_PATH, headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan_name"] == "free"
    assert body["entitlement_source"] == "external_billing"
    assert "addons" in body


def test_get_entitlement_requires_token(billing):
    resp = billing.client(_make_ws()).get(_PATH)
    assert resp.status_code == 401


def test_get_entitlement_not_found_returns_404(billing):
    resp = billing.client(workspace=None).get(_PATH, headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 404
