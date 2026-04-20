"""RBAC tests for billing routes (Issue #398).

Workspace admin/owner-only, session-only access on the two self-service
Stripe endpoints:

- ``POST /api/v1/billing/checkout``
- ``GET  /api/v1/billing/portal``

Session-only enforcement (require_workspace_admin_session) prevents a leaked
long-lived API key from initiating a Stripe checkout. The
``POST /api/v1/billing/webhook`` route remains open (Stripe signature
verification) and is intentionally not covered here.

The billing router is registered in ``api.main`` only when ``BILLING_ENABLED=true``.
This test mounts the router directly on a fresh FastAPI app so the suite runs
without environment toggles. The role gate is exercised in isolation by
overriding ``require_workspace_admin_session``.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from auth.dependencies import require_workspace_admin_session
from db.base import get_db
from plugins.billing.routes import router as billing_router

WORKSPACE_ID = uuid4()


def _mock_user(workspace_role: str) -> dict:
    return {
        "user_id": f"test_{workspace_role}",
        "email": f"{workspace_role}@test.com",
        "role": "user",
        "current_workspace_id": WORKSPACE_ID,
        "workspace_role": workspace_role,
    }


def _build_app() -> FastAPI:
    """Mount the billing router on a fresh app so tests are independent of BILLING_ENABLED."""
    app = FastAPI()
    app.include_router(billing_router)
    return app


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def non_admin_client():
    """Client where ``require_workspace_admin`` rejects with 403 (member or viewer)."""
    app = _build_app()

    async def mock_reject_admin():
        raise HTTPException(status_code=403, detail="Requires 'admin' role or higher")

    async def mock_db():
        yield MagicMock()

    app.dependency_overrides[require_workspace_admin_session] = mock_reject_admin
    app.dependency_overrides[get_db] = mock_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def admin_client():
    """Client where ``require_workspace_admin`` accepts (admin or owner)."""
    app = _build_app()
    user = _mock_user("admin")

    async def mock_admin():
        return user

    async def mock_db():
        yield MagicMock()

    app.dependency_overrides[require_workspace_admin_session] = mock_admin
    app.dependency_overrides[get_db] = mock_db
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


# ============================================================================
# Tests — denial path
# ============================================================================


class TestBillingNonAdminDenied:
    """Issue #398: billing checkout + portal must reject member/viewer roles."""

    def test_checkout_non_admin_gets_403(self, non_admin_client):
        response = non_admin_client.post(
            "/api/v1/billing/checkout",
            json={
                "plan_name": "pro",
                "success_url": "https://example.com/ok",
                "cancel_url": "https://example.com/cancel",
            },
        )
        assert response.status_code == 403, (
            f"checkout returned {response.status_code}, expected 403"
        )

    def test_portal_non_admin_gets_403(self, non_admin_client):
        response = non_admin_client.get(
            "/api/v1/billing/portal",
            params={"return_url": "https://example.com/back"},
        )
        assert response.status_code == 403, f"portal returned {response.status_code}, expected 403"


# ============================================================================
# Tests — admin/owner happy path
# ============================================================================


class TestBillingAdminHappyPath:
    """Smoke: an admin reaches the handler body. Proves the dep is wired, not bypassed."""

    def test_checkout_admin_reaches_handler(self, admin_client):
        with patch(
            "plugins.billing.routes.stripe_service.create_checkout_session",
            new=AsyncMock(return_value="https://stripe.test/session/abc"),
        ):
            response = admin_client.post(
                "/api/v1/billing/checkout",
                json={
                    "plan_name": "pro",
                    "success_url": "https://example.com/ok",
                    "cancel_url": "https://example.com/cancel",
                },
            )
        assert response.status_code == 200
        assert response.json() == {"checkout_url": "https://stripe.test/session/abc"}

    def test_portal_admin_reaches_handler(self, admin_client):
        with patch(
            "plugins.billing.routes.stripe_service.create_portal_session",
            new=AsyncMock(return_value="https://stripe.test/portal/xyz"),
        ):
            response = admin_client.get(
                "/api/v1/billing/portal",
                params={"return_url": "https://example.com/back"},
            )
        assert response.status_code == 200
        assert response.json() == {"portal_url": "https://stripe.test/portal/xyz"}
