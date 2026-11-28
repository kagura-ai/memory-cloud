"""The installation-bound read of the public usage statistics, without a database (#3035).

``GET /api/v1/workspaces/{wid}/sync-installations/{iid}/publication-usage-stats``:
the switch and its prerequisites answer 404 before authentication and run no
statement; the query is strict and refused before the first statement; the
capability is advertised only with its switch, is off by default and optional;
discovery with the switch off is the previous document; the route answers the
owner route's body and its OIDC calls count with the other sync controls.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql

from api.routes import public_mcp_usage, sync_installations
from auth import oidc_limits
from auth.sync_identity import SyncPrincipal, sync_reader
from config.settings import Settings, get_settings
from db.base import get_db
from services.sync_contract import (
    REQUIRED_SYNC_CAPABILITIES,
    USAGE_STATS_CAPABILITY,
    sync_configuration,
)
from utils.datetime import utcnow

SWITCHES = (
    "oidc_enabled",
    "mcp_public_endpoints_enabled",
    "sync_integrations_enabled",
    "sync_publication_usage_stats_enabled",
)
TOKEN = "kmc_oidc_at_" + "a" * 43
ROUTE = (
    "/api/v1/workspaces/{workspace_id}/sync-installations/{installation_id}/publication-usage-stats"
)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "sync-contract-v1.json"


def _path() -> str:
    return f"/api/v1/workspaces/{uuid4()}/sync-installations/{uuid4()}/publication-usage-stats"


@pytest.fixture
def app(monkeypatch):
    for name in SWITCHES:
        monkeypatch.setattr(get_settings(), name, True)
    monkeypatch.setattr(oidc_limits, "increment_counter", AsyncMock(return_value=1))
    application = FastAPI()
    application.include_router(sync_installations.router, prefix="/api/v1")
    return application


@pytest.fixture
def forbidden_db(app):
    opened: list[bool] = []

    async def forbidden():
        opened.append(True)
        raise AssertionError("the feature must answer before opening the database")
        yield

    app.dependency_overrides[get_db] = forbidden
    return opened


async def _get(app, path: str, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        return await c.get(path, **kwargs)


@pytest.mark.parametrize("credential", [None, "Bearer " + TOKEN])
@pytest.mark.parametrize("switch", SWITCHES)
async def test_the_route_is_404_before_authentication_while_the_capability_is_off(
    app, forbidden_db, monkeypatch, switch, credential
):
    monkeypatch.setattr(get_settings(), switch, False)
    response = await _get(app, _path(), headers={"Authorization": credential} if credential else {})
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    assert response.headers["cache-control"] == "no-store"
    assert forbidden_db == []


@pytest.mark.parametrize(
    "credential", [None, "Bearer kmc_rt_wrong", "Bearer ordinary-api-key", "Bearer session-token"]
)
async def test_other_credentials_are_refused_before_the_database(app, forbidden_db, credential):
    response = await _get(
        app,
        _path(),
        headers={"Authorization": credential} if credential else {},
        cookies={"session_token": "ignored-session"},
    )
    assert (response.status_code, response.json()) == (401, {"error": "invalid_token"})
    assert forbidden_db == []


@pytest.fixture
def session(app):
    """A reader whose identity check passed and a session that records every statement."""
    db = MagicMock()
    db.statements = []

    async def _execute(statement, *args, **kwargs):
        db.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        raise AssertionError("no statement runs for a refused query")

    db.execute = AsyncMock(side_effect=_execute)
    db.scalar = AsyncMock(side_effect=AssertionError("no statement runs for a refused query"))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def sessions():
        yield db

    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[sync_reader] = lambda: SyncPrincipal(
        "user-1", "client-1", "hash", "workspaces:read", utcnow()
    )
    return db


@pytest.mark.parametrize(
    "query",
    ["?days=0", "?days=91", "?days=x", "?days=1&days=2", "?installation_id=x", "?days=7&x=1"],
)
async def test_the_query_is_strict_and_refused_before_the_first_statement(app, session, query):
    response = await _get(app, _path() + query)
    assert (response.status_code, response.json()) == (400, {"error": "invalid_request"})
    assert response.headers["cache-control"] == "no-store"
    assert session.statements == []
    session.scalar.assert_not_called()


_CAPABILITY_FLAGS = (*SWITCHES, "sync_credentials_enabled", "sync_runs_enabled")


@pytest.mark.parametrize(
    "flag",
    ["oidc_enabled", "mcp_public_endpoints_enabled", "sync_integrations_enabled", SWITCHES[-1]],
)
def test_the_capability_needs_its_switch_and_the_installations(flag):
    configured = Settings.model_construct(**dict.fromkeys(_CAPABILITY_FLAGS, True))
    assert USAGE_STATS_CAPABILITY == "public.usage.stats.v1"
    assert USAGE_STATS_CAPABILITY in sync_configuration(configured).capabilities
    without = Settings.model_construct(**(dict.fromkeys(_CAPABILITY_FLAGS, True) | {flag: False}))
    assert USAGE_STATS_CAPABILITY not in sync_configuration(without).capabilities


def test_the_switch_is_off_by_default_and_the_capability_optional(monkeypatch):
    monkeypatch.delenv("SYNC_PUBLICATION_USAGE_STATS_ENABLED", raising=False)
    assert Settings(_env_file=None).sync_publication_usage_stats_enabled is False
    assert USAGE_STATS_CAPABILITY not in REQUIRED_SYNC_CAPABILITIES


def test_with_the_switch_off_discovery_is_the_previous_document():
    """Every other switch on: byte-identical to the document before the capability."""
    fixture = json.loads(FIXTURE.read_text())["configuration"]
    previous = {
        **fixture,
        "capabilities": [c for c in fixture["capabilities"] if c != USAGE_STATS_CAPABILITY],
    }
    flags = dict.fromkeys(_CAPABILITY_FLAGS, True) | {
        "sync_managed_enabled": True,
        "sync_observability_enabled": True,
        "mcp_public_usage_budget_enabled": True,
        "mcp_public_domains_enabled": True,
        "sync_publication_domain_operations_enabled": True,
        "sync_publication_text_operations_enabled": True,
        "sync_publication_usage_stats_enabled": False,
    }
    document = sync_configuration(Settings.model_construct(**flags)).model_dump_json()
    assert document == json.dumps(previous, separators=(",", ":"))
    flags["sync_publication_usage_stats_enabled"] = True
    on = sync_configuration(Settings.model_construct(**flags)).model_dump()
    assert on == fixture
    assert on["capabilities"][-1] == USAGE_STATS_CAPABILITY


def test_the_route_answers_the_owner_routes_body(app):
    schema = app.openapi()
    operation = schema["paths"][ROUTE]["get"]
    body = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert body == {"$ref": "#/components/schemas/PublicMcpUsageStatsResponse"}
    assert operation["tags"] == ["workspace-sync"]
    assert "experimental" in operation["description"].lower()
    owner = next(
        route
        for route in public_mcp_usage.router.routes
        if getattr(route, "path", "") == "/resources/{resource_id}/public-mcp/usage-stats"
    )
    route = next(r for r in sync_installations.router.routes if getattr(r, "path", "") == ROUTE[7:])
    assert route.response_model is owner.response_model  # type: ignore[attr-defined]
    days = next(p for p in operation["parameters"] if p["name"] == "days")
    assert days["schema"]["minimum"] == 1 and days["schema"]["maximum"] == 90
    assert days["schema"]["default"] == 7


def test_the_route_counts_with_the_other_sync_controls():
    path = (
        "/api/v1/workspaces/11111111-1111-1111-1111-111111111111/sync-installations/"
        "22222222-2222-2222-2222-222222222222/publication-usage-stats"
    )
    assert oidc_limits.rate_limit_operation(path) == "sync-human"
