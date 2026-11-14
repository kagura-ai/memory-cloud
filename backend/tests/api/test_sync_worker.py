"""Sync worker credentials cannot fall back to cookies or human namespaces."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.routes import sync_worker
from auth import oidc_limits
from config.settings import get_settings
from db.base import get_db


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Authorization", "Bearer kagura_wrong")],
        [("Authorization", "Bearer kmc_oidc_at_" + "A" * 43)],
        [("Authorization", "Bearer kmc_sync_at_short")],
        [
            ("Authorization", "Bearer kmc_sync_at_" + "A" * 43),
            ("Authorization", "Bearer duplicate"),
        ],
    ],
)
async def test_wrong_namespace_and_duplicate_headers_fail_before_database(headers, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "sync_integrations_enabled", True)
    monkeypatch.setattr(settings, "sync_credentials_enabled", True)
    monkeypatch.setattr(oidc_limits, "increment_counter", AsyncMock(return_value=1))
    app = FastAPI()
    app.include_router(sync_worker.router, prefix="/api/v1")

    async def no_database():
        pytest.fail("Invalid namespace reached the database")
        yield

    app.dependency_overrides[get_db] = no_database
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
        cookies={"session_id": "not-a-service-identity"},
    ) as client:
        response = await client.get(
            f"/api/v1/sync/installations/{uuid4()}",
            headers=headers,
        )
    assert response.status_code == 401
    assert response.json() == {"error": "invalid_token"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_burst_store_failure_refuses_service_admission_before_database(monkeypatch):
    settings = get_settings()
    for field in ("oidc_enabled", "sync_integrations_enabled", "sync_credentials_enabled"):
        monkeypatch.setattr(settings, field, True)
    monkeypatch.setattr(
        oidc_limits,
        "increment_counter",
        AsyncMock(side_effect=RuntimeError("sensitive-redis-connection")),
    )
    app = FastAPI()
    app.include_router(sync_worker.router, prefix="/api/v1")

    async def no_database():
        pytest.fail("Unavailable burst gate reached the database")
        yield

    app.dependency_overrides[get_db] = no_database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        response = await client.get(
            f"/api/v1/sync/installations/{uuid4()}",
            headers={"Authorization": "Bearer kmc_sync_at_" + "A" * 43},
        )
    assert response.status_code == 503
    assert response.json() == {"error": "temporarily_unavailable"}
    assert response.headers["cache-control"] == "no-store"
