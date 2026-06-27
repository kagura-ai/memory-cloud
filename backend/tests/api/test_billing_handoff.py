"""RBAC + behavior tests for the billing handoff endpoint (Issue #1093).

``POST /api/v1/billing/handoff`` is owner-only and session-only: it mints a
short-lived Ed25519 handoff token for the payment service. Mirrors
``tests/api/test_billing_rbac.py`` — the route is mounted on a fresh FastAPI app
and the auth gate is exercised in isolation via ``dependency_overrides`` so the
suite runs without Postgres or the full app harness.

Coverage:
- owner (session) → 200 with a payment ``url`` and ``expires_in == 120``;
- non-owner member → 403 (role gate);
- API-key / Bearer credential → 403 (session-only gate, real dependency chain);
- signing key unset → 503 HANDOFF-001 (fail-closed).
"""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from api.routes.billing_handoff import router as handoff_router
from auth.dependencies import require_workspace_owner_session
from config.settings import get_settings
from db.base import get_db
from utils.exceptions import MemoryCloudException

WORKSPACE_ID = uuid4()


def _mock_owner() -> dict:
    return {
        "user_id": "test_owner",
        "email": "owner@test.com",
        "role": "user",
        "current_workspace_id": WORKSPACE_ID,
        "workspace_role": "owner",
    }


async def _mock_db():
    yield MagicMock()


async def _mc_handler(request, exc: MemoryCloudException) -> JSONResponse:
    """Minimal stand-in for the production ``memory_cloud_exception_handler``.

    The fresh test app does not register the global handlers; this mirrors the
    status_code + error_code mapping so a route-raised MemoryCloudException
    surfaces as its real HTTP status (e.g. HANDOFF-001 → 503).
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error_code, "message": exc.message},
    )


def _build_app() -> FastAPI:
    """Mount the handoff router on a fresh app with the MemoryCloud handler."""
    app = FastAPI()
    app.include_router(handoff_router, prefix="/api/v1")
    app.add_exception_handler(MemoryCloudException, _mc_handler)
    return app


def _install_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the settings singleton at an ephemeral Ed25519 private key."""
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("utf-8")
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_handoff_signing_key", priv_pem)
    monkeypatch.setattr(settings, "billing_handoff_kid", "test-kid")
    monkeypatch.setattr(settings, "payment_public_base_url", "https://payment.example.com")


@pytest.fixture
def owner_client():
    """Client where the owner-only session gate accepts (owner)."""
    app = _build_app()
    app.dependency_overrides[require_workspace_owner_session] = _mock_owner
    app.dependency_overrides[get_db] = _mock_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def non_owner_client():
    """Client where the owner-only gate rejects with 403 (admin/member/viewer)."""
    app = _build_app()

    async def mock_reject():
        raise HTTPException(status_code=403, detail="Requires 'owner' role")

    app.dependency_overrides[require_workspace_owner_session] = mock_reject
    app.dependency_overrides[get_db] = _mock_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def bearer_client():
    """Client exercising the REAL owner-only dependency chain (no override).

    Only ``get_db`` is mocked. The real ``require_workspace_owner_session`` ->
    ``require_session_auth`` rejects any Bearer credential at the door (403)
    before any DB access, proving a leaked API key cannot mint a handoff.
    """
    app = _build_app()
    app.dependency_overrides[get_db] = _mock_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
    app.dependency_overrides.clear()


class TestBillingHandoffOwnerHappyPath:
    def test_owner_gets_200_with_url(self, owner_client, monkeypatch):
        _install_signing_key(monkeypatch)
        response = owner_client.post("/api/v1/billing/handoff")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["expires_in"] == 120
        assert body["url"].startswith("https://payment.example.com/enter?t=")
        # A real token (three dot-separated segments) is embedded.
        token = body["url"].split("?t=", 1)[1]
        assert token.count(".") == 2


class TestBillingHandoffDenied:
    def test_non_owner_gets_403(self, non_owner_client, monkeypatch):
        _install_signing_key(monkeypatch)  # set key so 403 is the role gate, not 503
        response = non_owner_client.post("/api/v1/billing/handoff")
        assert response.status_code == 403, response.text

    def test_bearer_api_key_gets_403(self, bearer_client, monkeypatch):
        _install_signing_key(monkeypatch)
        response = bearer_client.post(
            "/api/v1/billing/handoff",
            headers={"Authorization": "Bearer kagura_fake_api_key_value"},
        )
        assert response.status_code == 403, response.text


class TestBillingHandoffFailClosed:
    def test_unset_signing_key_gives_503(self, owner_client, monkeypatch):
        settings = get_settings()
        monkeypatch.setattr(settings, "billing_handoff_signing_key", "")
        monkeypatch.setattr(settings, "billing_handoff_kid", "")
        response = owner_client.post("/api/v1/billing/handoff")
        assert response.status_code == 503, response.text
        assert response.json()["error"] == "HANDOFF-001"
