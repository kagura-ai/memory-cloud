"""Trust boundary and wire validation for billing-owned integration rights."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from api.routes import internal_integrations
from config.settings import get_settings
from db.base import get_db
from utils.exceptions import MemoryCloudException

PATH = "/internal/workspaces/11111111-1111-4111-8111-111111111111/integration-entitlements"


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(get_settings(), "billing_service_token", "test-billing-service")
    app = FastAPI()
    app.include_router(internal_integrations.router)
    opened = []

    @app.exception_handler(MemoryCloudException)
    async def canonical_error(_request, exc):
        return JSONResponse({"error": exc.error_code}, status_code=exc.status_code)

    async def session():
        opened.append(True)
        yield AsyncMock()

    app.dependency_overrides[get_db] = session
    return app, opened


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential",
    [
        None,
        "Basic bad",
        "Bearer kmc_oidc_at_identity",
        "Bearer kagura_api_key",
        "Bearer resource-token",
    ],
)
async def test_untrusted_credentials_do_not_open_db(api, credential):
    headers = {"Cookie": "kagura_session=browser-session"}
    if credential:
        headers["Authorization"] = credential
    async with AsyncClient(
        transport=ASGITransport(app=api[0]), base_url="https://testserver"
    ) as client:
        response = await client.put(PATH, json={"revision": 1}, headers=headers)
    assert response.status_code == 401
    assert not api[1]


@pytest.mark.asyncio
async def test_missing_configuration_disables_billing_write(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "billing_service_token", "")
    async with AsyncClient(
        transport=ASGITransport(app=api[0]), base_url="https://testserver"
    ) as client:
        response = await client.get(PATH, headers={"Authorization": "Bearer test-billing-service"})
    assert response.status_code == 503
    assert not api[1]


@pytest.mark.asyncio
async def test_duplicate_billing_credentials_are_refused(api):
    async with AsyncClient(
        transport=ASGITransport(app=api[0]), base_url="https://testserver"
    ) as client:
        response = await client.get(
            PATH, headers=[("Authorization", "Bearer test-billing-service")] * 2
        )
    assert response.status_code == 401
    assert not api[1]


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-11-14T00:00:00", "2026-11-15T00:00:00Z"),
        ("2026-11-14T00:00:00Z", "2026-11-14T00:00:00Z"),
        ("2026-11-15T00:00:00Z", "2026-11-14T00:00:00Z"),
    ],
)
def test_windows_are_timezone_qualified_and_nonempty(start, end):
    with pytest.raises(ValidationError):
        internal_integrations.EntitlementWindow(
            valid_from=start, valid_until=end, evidence_ref="paid-term"
        )
