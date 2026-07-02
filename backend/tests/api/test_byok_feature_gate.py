"""Tests for the ENABLE_BYOK deployment flag (#1167).

``ENABLE_BYOK=false`` removes the BYOK surface from the deployment:

- every ``/external-keys`` CRUD endpoint returns 404 (feature-not-present
  semantics, consistent with plan_page #1145 — 403 would read as a
  permission problem and leak the feature's existence),
- ``GET /workspaces/{id}/cost-aggregation`` returns 404,
- ``GET /admin/cost-aggregation`` stays available (platform env-key usage
  still accrues cost the system admin may need to see).

The gate must run BEFORE auth/role dependencies so every caller — anonymous,
member, owner — sees the same 404 (no role-dependent 403-vs-404 split).
The anonymous-request tests below pin exactly that ordering: 404 when the
flag is off, the usual 401 when it is on.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app

_WORKSPACE_ID = uuid4()


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def byok_disabled(monkeypatch):
    """Rebuild the settings singleton with ENABLE_BYOK=false.

    ``monkeypatch`` restores both the env var and the original singleton
    instance on teardown, so other test modules see the default (BYOK on).
    """
    monkeypatch.setenv("ENABLE_BYOK", "false")
    monkeypatch.setattr("config.settings._settings", None)


class TestExternalKeysGate:
    def test_list_returns_404_when_disabled(self, client, byok_disabled):
        # Anonymous on purpose: the feature gate must fire before auth,
        # so even an unauthenticated caller sees 404 (not 401).
        response = client.get("/api/v1/external-keys")
        assert response.status_code == 404

    def test_create_returns_404_when_disabled(self, client, byok_disabled):
        response = client.post(
            "/api/v1/external-keys",
            json={"key_name": "OPENAI_API_KEY", "provider": "openai", "value": "sk-x"},
        )
        assert response.status_code == 404

    def test_update_returns_404_when_disabled(self, client, byok_disabled):
        response = client.put("/api/v1/external-keys/OPENAI_API_KEY", json={"value": "sk-y"})
        assert response.status_code == 404

    def test_delete_returns_404_when_disabled(self, client, byok_disabled):
        response = client.delete("/api/v1/external-keys/OPENAI_API_KEY")
        assert response.status_code == 404

    def test_routes_exist_when_enabled(self, client):
        # Default (flag on): the route exists, so an anonymous caller gets
        # the normal auth rejection — not 404.
        response = client.get("/api/v1/external-keys")
        assert response.status_code == 401


class TestCostAggregationGate:
    def test_workspace_route_returns_404_when_disabled(self, client, byok_disabled):
        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/cost-aggregation"
            "?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 404

    def test_workspace_route_exists_when_enabled(self, client):
        response = client.get(
            f"/api/v1/workspaces/{_WORKSPACE_ID}/cost-aggregation"
            "?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 401

    def test_admin_route_unaffected_when_disabled(self, client, byok_disabled):
        # /admin/cost-aggregation is intentionally NOT gated: anonymous gets
        # the normal 401 (route present), never the feature-gate 404.
        response = client.get(
            "/api/v1/admin/cost-aggregation?period=day&from=2026-04-01&to=2026-04-07"
        )
        assert response.status_code == 401


class TestSystemInfoByokFlag:
    def test_features_byok_defaults_on(self, client):
        features = client.get("/api/v1/system/info").json()["features"]
        assert features.get("byok") is True, "ENABLE_BYOK must default ON (OSS keeps BYOK)"

    def test_features_byok_reflects_disabled(self, client, byok_disabled):
        features = client.get("/api/v1/system/info").json()["features"]
        assert features.get("byok") is False
