"""Strict setup input and credential rejection before any authority SQL (#2878)."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.routes import sync_installations
from auth import oidc_limits
from config.settings import get_settings
from db.base import get_db

TOKEN = "kmc_oidc_at_" + "z" * 43
BODY = {
    "application_id": str(uuid4()),
    "application_revision": 1,
    "input_digest": "ab" * 32,
}


@pytest.fixture
def surface(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "sync_integrations_enabled", True)
    monkeypatch.setattr(settings, "oidc_rate_limit_per_minute", 60)
    counter = AsyncMock(return_value=1)
    monkeypatch.setattr(oidc_limits, "increment_counter", counter)
    identity = AsyncMock(side_effect=AssertionError("Invalid input must not reach authority SQL"))
    monkeypatch.setattr(sync_installations, "load_sync_identity", identity, raising=False)
    application = FastAPI()
    application.include_router(sync_installations.router, prefix="/api/v1")
    opened = []

    async def database():
        opened.append(True)
        yield object()

    application.dependency_overrides[get_db] = database
    return application, opened, identity, counter


async def post(surface, *, body=None, headers=None, query=""):
    async with AsyncClient(
        transport=ASGITransport(app=surface[0]), base_url="https://test"
    ) as client:
        return await client.post(
            f"/api/v1/workspaces/{uuid4()}/sync-billing-handoffs{query}",
            content=json.dumps(BODY).encode() if body is None else body,
            headers=(
                {"Authorization": "Bearer " + TOKEN, "content-type": "application/json"}
                if headers is None
                else headers
            ),
        )


@pytest.mark.parametrize(
    "credential",
    [None, "Bearer ordinary-api-key", "Bearer kmc_rt_wrong", "Bearer session-token"],
)
async def test_other_credential_namespaces_never_open_authority_db(surface, credential):
    headers = {"content-type": "application/json"}
    if credential is not None:
        headers["Authorization"] = credential
    response = await post(surface, headers=headers)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert not surface[1]


async def test_duplicate_authorization_is_not_a_second_chance(surface):
    response = await post(
        surface,
        headers=[
            ("content-type", "application/json"),
            ("Authorization", "Bearer " + TOKEN),
            ("Authorization", "Bearer " + TOKEN),
        ],
    )
    assert response.status_code == 401 and not surface[1]


async def test_default_off_does_not_parse_input_or_open_db(surface, monkeypatch):
    monkeypatch.setattr(get_settings(), "sync_integrations_enabled", False)
    response = await post(surface, body=b"{")
    assert response.status_code == 404
    assert response.headers["cache-control"] == "no-store"
    assert not surface[1] and not surface[2].await_count


@pytest.mark.parametrize(
    "changes",
    [
        {"application_revision": True},
        {"application_revision": "1"},
        {"application_revision": 0},
        {"application_revision": 2147483647},
        {"application_id": "not-an-application"},
        {"input_digest": "AB" * 32},
        {"input_digest": "ab" * 32 + "\n"},
        {"service_id": "browser-selected-service"},
        {"workspace_id": str(uuid4())},
        {"approved": True},
    ],
)
async def test_untrusted_application_fields_fail_before_sql(surface, changes):
    response = await post(surface, body=json.dumps(BODY | changes).encode())
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert not surface[2].await_count
    assert set(response.json()) == {"error"}


@pytest.mark.parametrize("kind", ["duplicate_key", "invalid_utf8", "utf16", "array", "nonfinite"])
async def test_ambiguous_or_non_utf8_json_is_not_normalized_into_consent(surface, kind):
    if kind == "duplicate_key":
        content = json.dumps(BODY)[:-1] + ',"application_revision":2}'
        raw = content.encode()
    elif kind == "invalid_utf8":
        raw = b'{"input_digest":"SENSITIVE_BODY\xff"}'
    elif kind == "utf16":
        raw = json.dumps(BODY).encode("utf-16")
    elif kind == "array":
        raw = b"[]"
    else:
        raw = json.dumps(BODY | {"application_revision": float("nan")}).encode()
    response = await post(surface, body=raw)
    assert response.status_code == 400
    assert not surface[2].await_count
    assert "SENSITIVE_BODY" not in response.text


async def test_stream_body_bound_precedes_json_or_sql(surface):
    response = await post(surface, body=b"SENSITIVE_BODY" * 400)
    assert response.status_code == 413 and not surface[2].await_count
    assert "SENSITIVE_BODY" not in response.text


@pytest.mark.parametrize("query", ["?approved=true", "?x=1&x=2"])
async def test_query_parameters_cannot_add_authority(surface, query):
    response = await post(surface, query=query)
    assert response.status_code == 400 and not surface[2].await_count


async def test_repeated_content_type_is_ambiguous(surface):
    response = await post(
        surface,
        headers=[
            ("Authorization", "Bearer " + TOKEN),
            ("content-type", "application/json"),
            ("content-type", "text/plain"),
        ],
    )
    assert response.status_code == 400 and not surface[2].await_count


async def test_slow_body_does_not_hold_authority_sql(surface, monkeypatch):
    monkeypatch.setattr(sync_installations, "SETUP_BODY_TIMEOUT_SECONDS", 0.01, raising=False)

    async def stream():
        yield b"{"
        await asyncio.sleep(60)

    response = await post(surface, body=stream())
    assert response.status_code == 400 and not surface[2].await_count


@pytest.mark.parametrize("failure", ["unavailable", "over_budget"])
async def test_setup_rate_guard_is_fail_closed_before_db(surface, failure):
    if failure == "unavailable":
        surface[3].side_effect = RuntimeError("SENSITIVE_SYNTHETIC_REDIS_URL")
        expected = 503
    else:
        surface[3].return_value = 61
        expected = 429
    response = await post(surface)
    assert response.status_code == expected and not surface[1]
    assert response.headers["cache-control"] == "no-store"
    assert "SENSITIVE_SYNTHETIC_REDIS_URL" not in response.text
