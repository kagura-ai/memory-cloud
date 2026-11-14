"""Credential boundaries for workspace-owned sync installation controls (#2802)."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.routes import internal_sync_installations, sync_installations
from auth import oidc_limits
from config.settings import get_settings
from db.base import get_db


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(get_settings(), "oidc_enabled", True)
    monkeypatch.setattr(get_settings(), "sync_integrations_enabled", True)
    monkeypatch.setattr(get_settings(), "billing_service_token", "fixture-billing-publisher")
    monkeypatch.setattr(oidc_limits, "increment_counter", AsyncMock(return_value=1))
    application = FastAPI()
    application.include_router(sync_installations.router, prefix="/api/v1")
    application.include_router(internal_sync_installations.router)
    opened = []

    async def forbidden_db():
        opened.append(True)
        raise AssertionError("Wrong credential namespace must fail before opening the DB")
        yield

    application.dependency_overrides[get_db] = forbidden_db
    return application, opened


@pytest.mark.parametrize(
    "credential", [None, "Bearer kmc_rt_wrong", "Bearer ordinary-api-key", "Bearer session-token"]
)
async def test_owner_controls_refuse_other_credentials_before_db(app, credential):
    application, opened = app
    async with AsyncClient(transport=ASGITransport(app=application), base_url="https://test") as c:
        response = await c.post(
            f"/api/v1/workspaces/{uuid4()}/sync-authorizations",
            headers={"Authorization": credential} if credential else {},
            cookies={"session_token": "ignored-session"},
            json={
                "request_id": str(uuid4()),
                "application_id": "application-1",
                "public_name": "Shop",
            },
        )
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert not opened


@pytest.mark.parametrize("credential", [None, "Bearer kmc_oidc_at_wrong", "Bearer worker-key"])
@pytest.mark.parametrize(
    "operation", ["sync-installations/activate", "sync-authorizations/validate"]
)
async def test_internal_activation_requires_the_billing_publisher(app, credential, operation):
    application, opened = app
    async with AsyncClient(transport=ASGITransport(app=application), base_url="https://test") as c:
        response = await c.post(
            f"/internal/workspaces/{uuid4()}/{operation}",
            headers={"Authorization": credential} if credential else {},
            json={"authorization_id": str(uuid4()), "application_id": "application-1"},
        )
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert not opened


async def test_duplicate_authorization_header_is_not_accepted(app):
    application, opened = app
    async with AsyncClient(transport=ASGITransport(app=application), base_url="https://test") as c:
        response = await c.post(
            f"/internal/workspaces/{uuid4()}/sync-installations/activate",
            headers=[("Authorization", "Bearer fixture-billing-publisher"), ("Authorization", "x")],
            json={"authorization_id": str(uuid4()), "application_id": "application-1"},
        )
    assert response.status_code == 401 and not opened


async def test_disabled_surface_is_private_and_does_not_open_db(app, monkeypatch):
    application, opened = app
    monkeypatch.setattr(get_settings(), "sync_integrations_enabled", False)
    async with AsyncClient(transport=ASGITransport(app=application), base_url="https://test") as c:
        response = await c.post(f"/api/v1/workspaces/{uuid4()}/sync-authorizations", json={})
    assert response.status_code == 404 and response.headers["cache-control"] == "no-store"
    assert not opened


async def test_missing_billing_configuration_fails_closed(app, monkeypatch):
    application, opened = app
    monkeypatch.setattr(get_settings(), "billing_service_token", "")
    async with AsyncClient(transport=ASGITransport(app=application), base_url="https://test") as c:
        response = await c.post(
            f"/internal/workspaces/{uuid4()}/sync-installations/activate", json={}
        )
    assert response.status_code == 503 and not opened
