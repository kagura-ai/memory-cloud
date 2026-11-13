"""HTTP authentication and failure contracts for identity-only workspace inspection."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import me_workspaces
from auth import oidc_limits
from config.settings import get_settings
from db.base import get_db

TOKEN = "kmc_oidc_at_" + "a" * 43


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(get_settings(), "oidc_enabled", True)
    monkeypatch.setattr(oidc_limits, "increment_counter", AsyncMock(return_value=1))
    app = FastAPI()
    app.include_router(me_workspaces.router, prefix="/api/v1")
    db = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.first.return_value = (
        "identity-subject",
        "openid workspaces:read",
        "openid workspaces:read",
        "openid workspaces:read",
    )
    result.all.return_value = []
    db.execute.return_value = result
    opened = []

    async def session():
        opened.append(True)
        yield db

    app.dependency_overrides[get_db] = session
    return app, db, opened


async def get(api, path="/api/v1/me/workspaces", headers=None, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=api[0]), base_url="https://testserver"
    ) as client:
        return await client.get(path, headers=headers, **kwargs)


def assert_private(response, status):
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Basic abc",
        "Bearer kagura_key",
        "Bearer " + "m" * 43,
        "Bearer kmc_oidc_rt_" + "r" * 43,
        "Bearer a.b.c",
        "Bearer kmc_oidc_at_short",
    ],
)
async def test_wrong_credentials_never_open_database_or_fall_back_to_cookie(api, authorization):
    headers = {"Cookie": "kagura_session=browser-session"}
    if authorization is not None:
        headers["Authorization"] = authorization
    response = await get(api, headers=headers)
    assert_private(response, 401)
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert not api[2]
    api[1].execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_authorization_is_rejected(api):
    response = await get(api, headers=[("Authorization", f"Bearer {TOKEN}")] * 2)
    assert_private(response, 401)
    assert not api[2]


@pytest.mark.asyncio
async def test_empty_memberships_keep_authenticated_subject(api):
    response = await get(api, headers={"Authorization": f"bEaReR {TOKEN}"})
    assert_private(response, 200)
    assert response.json() == {"subject": "identity-subject", "workspaces": [], "next_cursor": None}


@pytest.mark.asyncio
async def test_unknown_identity_and_narrowed_issuance_scope(api):
    api[1].execute.return_value.first.return_value = None
    response = await get(api, headers={"Authorization": f"Bearer {TOKEN}"})
    assert_private(response, 401)
    api[1].execute.return_value.first.return_value = (
        "identity-subject",
        "openid",
        "openid workspaces:read",
        "openid workspaces:read",
    )
    response = await get(api, headers={"Authorization": f"Bearer {TOKEN}"})
    assert_private(response, 403)
    assert 'error="insufficient_scope"' in response.headers["www-authenticate"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ["?limit=0", "?limit=101", "?limit=x", "?cursor=bad", "?subject=other", "?limit=1&limit=2"],
)
async def test_bad_query_is_safe_and_no_store(api, query):
    response = await get(
        api, "/api/v1/me/workspaces" + query, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert_private(response, 400)
    assert response.json() == {"error": "invalid_request"}


@pytest.mark.asyncio
async def test_dependency_failures_never_echo_exception_or_credential(api):
    api[1].execute.side_effect = RuntimeError(f"private-connection-error {TOKEN}")
    response = await get(api, headers={"Authorization": f"Bearer {TOKEN}"})
    assert_private(response, 503)
    assert response.json() == {"error": "temporarily_unavailable"}
    assert TOKEN not in response.text


@pytest.mark.asyncio
async def test_limiter_fails_closed_before_database(api, monkeypatch):
    monkeypatch.setattr(
        oidc_limits, "increment_counter", AsyncMock(side_effect=RuntimeError("down"))
    )
    response = await get(api, headers={"Authorization": f"Bearer {TOKEN}"})
    assert_private(response, 503)
    assert not api[2]


@pytest.mark.asyncio
async def test_database_session_startup_failure_is_safe(api):
    async def unavailable():
        raise RuntimeError("private startup error " + TOKEN)
        yield  # pragma: no cover

    api[0].dependency_overrides[get_db] = unavailable
    response = await get(api, headers={"Authorization": f"Bearer {TOKEN}"})
    assert_private(response, 503)
    assert response.json() == {"error": "temporarily_unavailable"}


@pytest.mark.asyncio
async def test_disabled_feature_never_opens_database(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "oidc_enabled", False)
    response = await get(api, headers={"Authorization": f"Bearer {TOKEN}"})
    assert_private(response, 404)
    assert not api[2]


@pytest.mark.asyncio
async def test_rate_limit_before_database_is_private(api, monkeypatch):
    monkeypatch.setattr(oidc_limits, "increment_counter", AsyncMock(return_value=10**9))
    response = await get(api, headers={"Authorization": f"Bearer {TOKEN}"})
    assert_private(response, 429)
    assert int(response.headers["retry-after"]) > 0
    assert not api[2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix",
    [
        "invalid/access",
        "00000000-0000-0000-0000-000000000000/access?context_id=secret-input",
        "00000000-0000-0000-0000-000000000000/access?context_id=x&context_id=y",
    ],
)
async def test_access_validation_never_reflects_input(api, suffix):
    response = await get(
        api, "/api/v1/me/workspaces/" + suffix, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert_private(response, 400)
    assert response.json() == {"error": "invalid_request"}
