"""API tests for admin plan tiers listing endpoint (Issue #664).

Direct TestClient pattern (mirrors ``test_admin_signup_gate``). The endpoint
returns process-global ``PLAN_TIERS`` config and never touches the DB, so the
``get_db`` override returns a MagicMock — DB calls in the handler would be
a regression caught by ``MagicMock`` failing on awaited calls.

Env-override coverage is via direct ``PLAN_TIERS`` monkeypatch because the
override map is applied at module import time; mutating ``os.environ`` after
import is a no-op (see ``config.plan_tiers._apply_settings_overrides``).
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import require_admin
from db.base import get_db


def _mock_admin_user() -> dict:
    return {"user_id": "admin_runner", "email": "admin@test.invalid", "role": "admin"}


@pytest.fixture
def client():
    async def mock_admin():
        return _mock_admin_user()

    async def mock_db():
        yield MagicMock()

    app.dependency_overrides[require_admin] = mock_admin
    app.dependency_overrides[get_db] = mock_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def test_list_plan_tiers_returns_three_tiers_in_canonical_order(client: TestClient) -> None:
    resp = client.get("/api/v1/admin/plans/tiers")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert [t["name"] for t in data] == ["free", "basic", "pro"]


def test_list_plan_tiers_exposes_pivot_corrected_mcp_quota(client: TestClient) -> None:
    """The renamed ``apiCalls`` row must show actual ``mcp_calls_per_day``
    (not the legacy ``daily_api_limit``), since the frontend now binds to
    this field. Lock the rename in.
    """
    resp = client.get("/api/v1/admin/plans/tiers")
    assert resp.status_code == 200
    free, basic, pro = resp.json()

    assert free["mcp_calls_per_day"] == 1000
    assert basic["mcp_calls_per_day"] == 10000
    assert pro["mcp_calls_per_day"] == 50000

    # Legacy field must not leak into the public surface — admins should
    # not see ``daily_api_limit`` numbers (100/2000/10000) that diverge
    # from the actual quota enforced server-side.
    assert "daily_api_limit" not in free
    assert "weekly_api_limit" not in basic


def test_list_plan_tiers_serializes_pro_only_features(client: TestClient) -> None:
    resp = client.get("/api/v1/admin/plans/tiers")
    free, basic, pro = resp.json()

    # Features arrive sorted for deterministic output.
    assert pro["features"] == sorted(pro["features"])

    # PRO-exclusive features
    for pro_only in ("memory_agent", "memory_analysis", "public_contexts", "shared_contexts"):
        assert pro_only in pro["features"]
        assert pro_only not in free["features"]
        assert pro_only not in basic["features"]

    # Reranking: BASIC+ only
    assert "reranking" in basic["features"]
    assert "reranking" not in free["features"]

    # OAuth (MCP App Credentials): all tiers
    for tier in (free, basic, pro):
        assert "oauth" in tier["features"]


def test_list_plan_tiers_exposes_new_quota_fields(client: TestClient) -> None:
    """Cover the 10 new rows added by #664 (data side — values come from
    the dataclass, which already had these fields)."""
    resp = client.get("/api/v1/admin/plans/tiers")
    free, basic, pro = resp.json()

    # storage_limit_bytes
    assert free["storage_limit_bytes"] == 100 * 1024 * 1024
    assert basic["storage_limit_bytes"] == 1024 * 1024 * 1024
    assert pro["storage_limit_bytes"] == 10 * 1024 * 1024 * 1024

    # max_members_per_workspace
    assert free["max_members_per_workspace"] == 1
    assert pro["max_members_per_workspace"] == 10

    # max_resource_tokens (zero-floored on FREE)
    assert free["max_resource_tokens"] == 0
    assert pro["max_resource_tokens"] == 30

    # REST + public + bound-public PRO-only rate-limits
    assert free["rest_calls_per_day"] == 0
    assert pro["rest_calls_per_day"] == 5000
    assert pro["public_calls_per_day"] == 1000
    assert pro["bound_public_calls_per_minute"] == 100

    # sleep_enabled_contexts_limit (PRO-only)
    assert free["sleep_enabled_contexts_limit"] == 0
    assert pro["sleep_enabled_contexts_limit"] == 3

    # allows_shared_contexts (PRO-only)
    assert free["allows_shared_contexts"] is False
    assert pro["allows_shared_contexts"] is True


def test_list_plan_tiers_reflects_runtime_override(client: TestClient, monkeypatch) -> None:
    """``_apply_settings_overrides`` runs at import time, so we monkeypatch
    ``PLAN_TIERS`` directly to simulate an env-driven override taking
    effect. Locks in the "values from server, not hardcoded" contract.
    """
    import config.plan_tiers as plan_tiers_module

    overridden = dataclasses.replace(plan_tiers_module.PLAN_TIERS["free"], memory_limit=42)
    monkeypatch.setitem(plan_tiers_module.PLAN_TIERS, "free", overridden)

    resp = client.get("/api/v1/admin/plans/tiers")
    assert resp.status_code == 200
    free = resp.json()[0]
    assert free["memory_limit"] == 42


# Route-level negative-auth assertion intentionally omitted: this repo's
# admin-test pattern (see ``test_admin_signup_gate.py``) trusts the
# ``require_admin`` dependency, whose own behavior is covered by tests in
# ``tests/auth/``. Exercising the unauthenticated path here would route
# through SessionMiddleware → Redis, leaking the test out of the api-tier
# unit boundary (and failing in local environments without a running
# Redis container).
