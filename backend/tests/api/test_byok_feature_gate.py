"""Tests for the ENABLE_BYOK deployment flag (#1167).

``ENABLE_BYOK=false`` disables BYOK PROVISIONING but (v0.42 review #32) keeps
key MANAGEMENT reachable so a customer can still disable/delete a key that is
still being resolved (and billed):

- the ``/external-keys`` WRITE paths (POST create, PUT update) return 404
  (feature-not-present semantics, consistent with plan_page #1145 — 403 would
  read as a permission problem and leak the feature's existence),
- the ``/external-keys`` MANAGEMENT paths (GET list, PATCH toggle, DELETE)
  stay available so an owner retains control of already-stored keys; an
  anonymous caller gets the normal 401 (route present),
- ``GET /workspaces/{id}/cost-aggregation`` returns 404,
- ``GET /admin/cost-aggregation`` stays available (platform env-key usage
  still accrues cost the system admin may need to see).

On the write paths the gate must run BEFORE auth/role dependencies so every
caller sees the same 404 (no role-dependent 403-vs-404 split). The
anonymous-request tests below pin exactly that ordering: 404 for writes when
the flag is off, the usual 401 for management and when the flag is on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from auth.dependencies import get_user_from_api_key_or_session, require_workspace_owner
from db.base import get_db

_WORKSPACE_ID = uuid4()


@pytest.fixture
def client():
    """TestClient that auto-clears dependency overrides on teardown."""
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
    # --- WRITE paths: 404 before auth when disabled (no existence leak) ---
    def test_create_returns_404_when_disabled(self, client, byok_disabled):
        # Anonymous on purpose: the write gate must fire before auth,
        # so even an unauthenticated caller sees 404 (not 401).
        response = client.post(
            "/api/v1/external-keys",
            json={"key_name": "OPENAI_API_KEY", "provider": "openai", "value": "sk-x"},
        )
        assert response.status_code == 404

    def test_update_returns_404_when_disabled(self, client, byok_disabled):
        response = client.put("/api/v1/external-keys/OPENAI_API_KEY", json={"value": "sk-y"})
        assert response.status_code == 404

    # --- MANAGEMENT paths: reachable when disabled so keys stay controllable
    # (v0.42 review #32). Anonymous → normal 401 (route present), not 404. ---
    def test_list_available_when_disabled(self, client, byok_disabled):
        response = client.get("/api/v1/external-keys")
        assert response.status_code == 401

    def test_delete_available_when_disabled(self, client, byok_disabled):
        response = client.delete("/api/v1/external-keys/OPENAI_API_KEY")
        assert response.status_code == 401

    def test_toggle_available_when_disabled(self, client, byok_disabled):
        response = client.patch(
            "/api/v1/external-keys/OPENAI_API_KEY/toggle", json={"enabled": False}
        )
        assert response.status_code == 401

    def test_routes_exist_when_enabled(self, client):
        # Default (flag on): the route exists, so an anonymous caller gets
        # the normal auth rejection — not 404.
        response = client.get("/api/v1/external-keys")
        assert response.status_code == 401


class TestOwnerWithdrawsStoredKeyWhenDisabled:
    """#1613: BYOK off must not strand a credential in the database.

    The management routes were already reachable (above), but ``DELETE`` still
    answered 400 for ``OPENAI_API_KEY`` — the one key an owner is most likely
    to have stored. With provisioning off the key can be neither replaced nor
    re-registered, so it is never protected: the owner lists it, sees
    ``is_protected=false`` and deletes it, while create/update stay 404.
    """

    @pytest.fixture
    def stored_key(self):
        now = datetime.now(UTC)
        key = MagicMock()
        key.id = 1
        key.key_name = "OPENAI_API_KEY"
        key.provider = "openai"
        key.encrypted_value = "not-decryptable"
        key.user_id = "owner_1"
        key.enabled = True
        key.created_at = now
        key.updated_at = now
        return key

    @pytest.fixture
    def owner_client(self, byok_disabled, stored_key):
        user = {
            "user_id": "owner_1",
            "email": "owner@test.invalid",
            "role": "user",
            "current_workspace_id": _WORKSPACE_ID,
            "workspace_role": "owner",
        }
        result = MagicMock()
        result.scalar_one_or_none.return_value = stored_key
        result.scalars.return_value.all.return_value = [stored_key]
        db = AsyncMock()
        db.execute.return_value = result

        async def _user():
            return user

        async def _owner():
            return (user["user_id"], _WORKSPACE_ID)

        async def _db():
            yield db

        app.dependency_overrides[get_user_from_api_key_or_session] = _user
        app.dependency_overrides[require_workspace_owner] = _owner
        app.dependency_overrides[get_db] = _db
        client = TestClient(app, raise_server_exceptions=False)
        client.db = db  # type: ignore[attr-defined]
        yield client
        app.dependency_overrides.clear()

    def test_owner_lists_the_key_as_unprotected(self, owner_client):
        response = owner_client.get("/api/v1/external-keys")
        assert response.status_code == 200
        (key,) = response.json()["keys"]
        assert key["key_name"] == "OPENAI_API_KEY"
        assert key["is_protected"] is False

    def test_owner_deletes_the_openai_key(self, owner_client, stored_key):
        response = owner_client.delete("/api/v1/external-keys/OPENAI_API_KEY")
        assert response.status_code == 200, response.text
        owner_client.db.delete.assert_awaited_once_with(stored_key)
        owner_client.db.commit.assert_awaited()

    def test_create_and_update_stay_closed_for_the_owner(self, owner_client):
        created = owner_client.post(
            "/api/v1/external-keys",
            json={"key_name": "OPENAI_API_KEY", "provider": "openai", "value": "sk-x"},
        )
        updated = owner_client.put("/api/v1/external-keys/OPENAI_API_KEY", json={"value": "sk-y"})
        assert (created.status_code, updated.status_code) == (404, 404)
        owner_client.db.commit.assert_not_awaited()


class TestOpenAIKeyStatusGate:
    """The key-status probe is a BYOK read surface too (#1167 gate1 finding).

    With BYOK off it must 404: leaving it up would report has_key=false in a
    deployment where env keys serve embeddings, and the contexts page blocks
    context creation on has_key=false. Both frontend consumers degrade
    gracefully on a failed probe (contexts page → null → creation enabled;
    OnboardingCard → .catch(() => null) → normal onboarding).
    """

    def test_returns_404_when_disabled(self, client, byok_disabled):
        response = client.get(f"/api/v1/workspaces/{_WORKSPACE_ID}/openai-key-status")
        assert response.status_code == 404

    def test_route_exists_when_enabled(self, client):
        response = client.get(f"/api/v1/workspaces/{_WORKSPACE_ID}/openai-key-status")
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
