"""Unit tests for GET /internal/workspaces/{id}/downgrade-eligibility (#1123).

TestClient + mocked DB (mirrors test_internal_billing_plan.py). The eligibility
math is unit-tested in tests/services/test_downgrade_eligibility_service.py; here
we pin the route: service-token auth, soft-delete 404, bad-id 422, and that the
handler wires the service + serializes the per-tier blockers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from db.base import get_db

WS_ID = str(uuid4())
_PATH = f"/internal/workspaces/{WS_ID}/downgrade-eligibility"


def _make_ws(plan_name: str = "pro"):
    ws = MagicMock()
    ws.id = uuid4()
    ws.plan_name = plan_name
    ws.deleted_at = None
    ws.addon_member_bonus = 0
    ws.addon_context_bonus = 0
    ws.addon_memory_bonus = 0
    return ws


def _ws_result(ws):
    r = MagicMock()
    r.scalar_one_or_none.return_value = ws
    return r


def _count(n: int):
    r = MagicMock()
    r.scalar.return_value = n
    return r


@pytest.fixture
def billing(monkeypatch):
    """Harness mirroring test_internal_billing_plan.py."""
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

        def client(self, execute_results=None) -> TestClient:
            async def mock_db():
                db = MagicMock()
                db.execute = AsyncMock(side_effect=execute_results)
                yield db

            app.dependency_overrides[get_db] = mock_db
            return TestClient(app, raise_server_exceptions=False)

    yield _Harness()
    app.dependency_overrides.clear()


def test_requires_bearer_token(billing):
    resp = billing.client().get(_PATH)
    assert resp.status_code == 401


def test_invalid_token_returns_401(billing):
    resp = billing.client().get(_PATH, headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_unset_token_returns_503(billing):
    billing.token = ""
    resp = billing.client().get(_PATH, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 503


def test_invalid_workspace_id_returns_422(billing):
    resp = billing.client().get(
        "/internal/workspaces/not-a-uuid/downgrade-eligibility",
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 422


def test_missing_workspace_returns_404(billing):
    resp = billing.client(execute_results=[_ws_result(None)]).get(
        _PATH, headers={"Authorization": "Bearer secret"}
    )
    assert resp.status_code == 404


def test_pro_with_low_usage_is_eligible_for_all_lower_tiers(billing):
    ws = _make_ws("pro")
    # ws load, then members/contexts/shared/memories/tokens.
    results = [_ws_result(ws), _count(1), _count(1), _count(0), _count(10), _count(0)]
    resp = billing.client(execute_results=results).get(
        _PATH, headers={"Authorization": "Bearer secret"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["current_plan"] == "pro"
    assert [t["target_plan"] for t in body["targets"]] == ["free", "basic"]
    assert all(t["eligible"] for t in body["targets"])
    assert all(t["blockers"] == [] for t in body["targets"])


def test_pro_with_high_usage_is_blocked(billing):
    ws = _make_ws("pro")
    # 5 members, 25 contexts, 2 shared, 10 memories, 0 tokens.
    results = [_ws_result(ws), _count(5), _count(25), _count(2), _count(10), _count(0)]
    resp = billing.client(execute_results=results).get(
        _PATH, headers={"Authorization": "Bearer secret"}
    )
    assert resp.status_code == 200
    by_plan = {t["target_plan"]: t for t in resp.json()["targets"]}
    free_dims = {b["dimension"] for b in by_plan["free"]["blockers"]}
    assert free_dims == {"members", "contexts", "shared_contexts"}
    assert by_plan["free"]["eligible"] is False
    members_blocker = next(b for b in by_plan["free"]["blockers"] if b["dimension"] == "members")
    assert members_blocker["cleanup"] == "remove_members"
    assert members_blocker["overage"] == 4  # 5 - free limit(1)


def test_free_workspace_has_no_downgrade_targets(billing):
    ws = _make_ws("free")
    # Only the workspace load runs — evaluate() short-circuits before counting.
    resp = billing.client(execute_results=[_ws_result(ws)]).get(
        _PATH, headers={"Authorization": "Bearer secret"}
    )
    assert resp.status_code == 200
    assert resp.json()["targets"] == []
