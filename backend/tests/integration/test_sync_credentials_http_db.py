"""Exercise real /oidc/token dispatch and the isolated service consumer."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from starlette.requests import Request

from api.routes import oidc, sync_worker
from auth.dependencies import get_user_from_api_key_or_session
from models.sync_credential import SyncAccessToken
from tests.integration import sync_credential_fixtures as fixtures
from tests.integration.sync_credential_fixtures import token_form

pytestmark = pytest.mark.asyncio(loop_scope="session")
store = fixtures.store
worker = fixtures.worker


async def request(worker, method, path, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=worker.store.app), base_url="https://test"
    ) as client:
        return await client.request(method, path, **kwargs)


def routes(worker):
    worker.store.app.include_router(oidc.router, prefix="/oidc")
    worker.store.app.include_router(sync_worker.router, prefix="/api/v1")


async def test_actual_token_exchange_and_scoped_consumer_never_return_human_or_refresh_tokens(
    worker,
):
    routes(worker)
    form = token_form(worker)
    response = await request(worker, "POST", "/oidc/token", data=form)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert set(result) == {"access_token", "expires_in", "token_type", "scope"}
    info = await request(
        worker,
        "GET",
        "/api/v1/sync/installations/" + worker.target["installation_id"],
        headers={"Authorization": "Bearer " + result["access_token"]},
    )
    assert info.status_code == 200, info.text
    assert info.json()["workspace_id"] == str(worker.store.wid)
    assert "user_id" not in info.json()
    replay = await request(worker, "POST", "/oidc/token", data=form)
    assert replay.status_code == 400 and replay.json() == {"error": "invalid_client"}


@pytest.mark.parametrize(
    "kind",
    [
        "authorization",
        "query",
        "duplicate",
        "wrong_scope",
        "wrong_target",
        "human_client",
        "extra_secret",
        "wrong_grant",
    ],
)
async def test_service_grant_has_no_alternative_credential_or_target_path(worker, kind):
    routes(worker)
    form, options, path = token_form(worker), {}, "/oidc/token"
    if kind == "authorization":
        options["headers"] = {"Authorization": "Bearer " + worker.store.tokens["owner"]}
    elif kind == "query":
        path += "?client_id=other"
    elif kind == "duplicate":
        from urllib.parse import urlencode

        options.update(
            content=urlencode(form) + "&scope=resource.sync",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    elif kind == "wrong_scope":
        form["scope"] = "openid memory:admin"
    elif kind == "wrong_target":
        form["resource"] = fixtures.ISSUER + "/sync/installations/" + str(uuid4())
    elif kind == "human_client":
        form["client_id"] = worker.store.client_id
    elif kind == "extra_secret":
        form["client_secret"] = "not-a-service-credential"
    else:
        form["grant_type"] = "authorization_code"
    if kind != "duplicate":
        options["data"] = form
    response = await request(worker, "POST", path, **options)
    assert response.status_code == 400, response.text
    assert response.headers["cache-control"] == "no-store"
    assert "client_assertion" not in response.text
    async with worker.factory() as db:
        assert await db.scalar(select(func.count()).select_from(SyncAccessToken)) == 0


async def test_service_bearer_cannot_become_a_general_api_or_session_user(worker):
    from tests.integration.sync_credential_fixtures import mint

    result = await mint(worker)
    request = Request({"type": "http", "method": "GET", "path": "/api/v1/contexts", "headers": []})
    request.state.user = {"user_id": worker.store.users["owner"], "email": "owner@example.test"}
    async with worker.factory() as db:
        with pytest.raises(HTTPException) as exc:
            await get_user_from_api_key_or_session(request, api_key=result["access_token"], db=db)
    assert exc.value.status_code == 401


async def test_replay_store_outage_is_closed_and_does_not_echo_diagnostics(worker, monkeypatch):
    from services import sync_credentials

    routes(worker)
    monkeypatch.setattr(
        sync_credentials,
        "_consume_assertion",
        AsyncMock(side_effect=RuntimeError("sensitive-connection-diagnostics")),
    )
    response = await request(worker, "POST", "/oidc/token", data=token_form(worker))
    assert response.status_code == 503
    assert response.json() == {"error": "temporarily_unavailable"}
    async with worker.factory() as db:
        assert await db.scalar(select(func.count()).select_from(SyncAccessToken)) == 0


async def test_discovery_advertises_service_profile_only_while_enabled(worker, monkeypatch):
    from types import SimpleNamespace

    from config.settings import get_settings

    routes(worker)
    monkeypatch.setattr(oidc, "load_oidc_config", lambda: SimpleNamespace(issuer=fixtures.ISSUER))
    response = await request(worker, "GET", "/oidc/.well-known/openid-configuration")
    assert response.status_code == 200
    metadata = response.json()
    assert "client_credentials" in metadata["grant_types_supported"]
    assert "private_key_jwt" in metadata["token_endpoint_auth_methods_supported"]
    assert "resource.sync" in metadata["scopes_supported"]
    assert metadata["token_endpoint_auth_signing_alg_values_supported"] == ["RS256"]
    assert metadata["token_endpoint"] == fixtures.ISSUER + "/token"
    monkeypatch.setattr(get_settings(), "sync_credentials_enabled", False)
    response = await request(worker, "GET", "/oidc/.well-known/openid-configuration")
    assert "client_credentials" not in response.json()["grant_types_supported"]
    assert "private_key_jwt" not in response.json()["token_endpoint_auth_methods_supported"]
    refused = await request(worker, "POST", "/oidc/token", data=token_form(worker))
    assert refused.status_code == 400
    assert refused.json() == {"error": "unsupported_grant_type"}
