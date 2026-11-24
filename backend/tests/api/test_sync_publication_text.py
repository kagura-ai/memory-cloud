"""A managing application sets the endpoint's public text, without a database (#2995).

``POST /api/v1/workspaces/{wid}/sync-installations/{iid}/publication-text``:
the switch and its prerequisites, the credential namespace and the request
body are refused before the feature runs a statement; the body keeps the
owner API's limits; the service module creates no publication and touches
no paid right; the route has its own OIDC bucket.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql

from api.routes import sync_installations
from auth import oidc_limits
from auth.oidc_bearer import identity_access_token
from config.settings import Settings, get_settings
from db.base import get_db
from models.public_mcp import PUBLIC_DESCRIPTION_MAX_CHARS, PUBLIC_USAGE_GUIDE_MAX_CHARS
from services.sync_contract import (
    REQUIRED_SYNC_CAPABILITIES,
    TEXT_OPERATIONS_CAPABILITY,
    sync_configuration,
)

SWITCHES = (
    "oidc_enabled",
    "mcp_public_endpoints_enabled",
    "sync_integrations_enabled",
    "sync_publication_text_operations_enabled",
)
SRC = Path(__file__).resolve().parents[2] / "src"
TOKEN = "kmc_oidc_at_" + "a" * 43


def _path() -> str:
    return f"/api/v1/workspaces/{uuid4()}/sync-installations/{uuid4()}/publication-text"


def _binding() -> dict:
    return {
        "application_id": str(uuid4()),
        "resource_id": "sync-" + uuid4().hex,
        "resource_pk": str(uuid4()),
        "context_id": str(uuid4()),
        "endpoint_id": str(uuid4()),
        "public_id": "pmcp_AbCdEfGhIjKlMnOpQrStUv",
        "generation": 1,
    }


def _body(**fields) -> dict:
    return {"expected": _binding(), **({"public_description": "Example Shop"} | fields)}


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


async def _post(app, path: str, body: object = None, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        if body is None:
            return await c.post(path, **kwargs)
        return await c.post(path, json=body, **kwargs)


@pytest.mark.parametrize("credential", [None, "Bearer " + TOKEN])
@pytest.mark.parametrize("switch", SWITCHES)
async def test_the_route_is_404_before_authentication_while_the_capability_is_off(
    app, forbidden_db, monkeypatch, switch, credential
):
    monkeypatch.setattr(get_settings(), switch, False)
    response = await _post(
        app, _path(), _body(), headers={"Authorization": credential} if credential else {}
    )
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    assert response.headers["cache-control"] == "no-store"
    assert forbidden_db == []


@pytest.mark.parametrize(
    "credential", [None, "Bearer kmc_rt_wrong", "Bearer ordinary-api-key", "Bearer session-token"]
)
async def test_other_credentials_are_refused_before_the_database(app, forbidden_db, credential):
    response = await _post(
        app,
        _path(),
        _body(),
        headers={"Authorization": credential} if credential else {},
        cookies={"session_token": "ignored-session"},
    )
    assert (response.status_code, response.json()) == (401, {"error": "invalid_token"})
    assert forbidden_db == []


class _Empty:
    rowcount = 0

    def scalar(self):
        return None

    def scalar_one_or_none(self):
        return None

    def first(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []


@pytest.fixture
def session(app):
    """A session that answers every statement with no row and keeps the SQL."""
    db = MagicMock()
    db.statements = []

    async def _execute(statement, *args, **kwargs):
        db.statements.append(str(statement.compile(dialect=postgresql.dialect())))
        return _Empty()

    db.execute = AsyncMock(side_effect=_execute)
    db.scalar = AsyncMock(return_value=None)
    db.get = AsyncMock(return_value=None)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()

    async def sessions():
        yield db

    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[identity_access_token] = lambda: TOKEN
    return db


def _without(key: str) -> dict:
    body = _body()
    body.pop(key)
    return body


_STRICT_CASES = [
    ("", _body(public_name="Renamed"), 400),
    ("", _body(extra=1), 400),
    ("", {"expected": _binding()}, 400),
    ("", _without("expected"), 400),
    ("", {**_body(), "expected": {**_binding(), "generation": "1"}}, 400),
    ("", _body(public_description="d" * (PUBLIC_DESCRIPTION_MAX_CHARS + 1)), 400),
    ("", _body(public_usage_guide="g" * (PUBLIC_USAGE_GUIDE_MAX_CHARS + 1)), 400),
    ("", _body(public_description=7), 400),
    # #2995: a NUL is refused here, never at the database flush (a retryable 503).
    ("", _body(public_description="a\x00b"), 400),
    ("", _body(public_usage_guide="\x00"), 400),
    ("?installation_id=x", _body(), 400),
    ("", _body(public_usage_guide="x" * 99_000), 413),
]


@pytest.mark.parametrize(("query", "body", "status"), _STRICT_CASES)
async def test_bodies_are_strict_and_refused_before_the_first_statement(
    app, session, query, body, status
):
    response = await _post(app, _path() + query, body)
    expected = {"error": "request_too_large" if status == 413 else "invalid_request"}
    assert (response.status_code, response.json()) == (status, expected)
    assert response.headers["cache-control"] == "no-store"
    assert session.statements == []
    session.scalar.assert_not_called()
    session.get.assert_not_called()


async def test_a_body_that_is_not_json_is_refused(app, session):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        response = await c.post(
            _path(), content=json.dumps(_body()), headers={"content-type": "text/plain"}
        )
    assert (response.status_code, response.json()) == (400, {"error": "invalid_request"})
    assert session.statements == []
    session.scalar.assert_not_called()


@pytest.mark.parametrize(
    "fields",
    [
        {"public_description": "d" * PUBLIC_DESCRIPTION_MAX_CHARS},
        {"public_usage_guide": "g" * PUBLIC_USAGE_GUIDE_MAX_CHARS, "public_description": None},
        # The worst case of JSON escaping at both limits stays under the byte cap.
        {
            "public_description": "\U0001f600" * (PUBLIC_DESCRIPTION_MAX_CHARS // 2),
            "public_usage_guide": "\U0001f600" * (PUBLIC_USAGE_GUIDE_MAX_CHARS // 2),
        },
        {"public_description": "", "public_usage_guide": None},
    ],
)
async def test_a_body_within_the_limits_reaches_the_identity_check(app, session, fields):
    """No token row in this session: the body passed, the identity check answers 401."""
    body = {"expected": _binding(), **fields}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        response = await c.post(
            _path(),
            content=json.dumps(body, ensure_ascii=True),
            headers={"content-type": "application/json"},
        )
    assert (response.status_code, response.json()) == (401, {"error": "invalid_token"})
    assert session.statements != []


def test_the_route_has_its_own_oidc_bucket():
    path = (
        "/api/v1/workspaces/11111111-1111-1111-1111-111111111111/sync-installations/"
        "22222222-2222-2222-2222-222222222222/publication-text"
    )
    assert oidc_limits.rate_limit_operation(path) == "publication-text"
    # Its neighbours keep theirs.
    assert oidc_limits.rate_limit_operation(path + "/x") == "sync-human"
    assert (
        oidc_limits.rate_limit_operation(path.replace("publication-text", "publication-domains"))
        == "sync-human"
    )


_CAPABILITY_FLAGS = (*SWITCHES, "sync_credentials_enabled", "sync_runs_enabled")


@pytest.mark.parametrize(
    "flag",
    ["oidc_enabled", "mcp_public_endpoints_enabled", "sync_integrations_enabled", SWITCHES[-1]],
)
def test_the_capability_needs_its_switch_and_the_installations(flag):
    configured = Settings.model_construct(**dict.fromkeys(_CAPABILITY_FLAGS, True))
    assert TEXT_OPERATIONS_CAPABILITY == "public.text.operations.v1"
    assert TEXT_OPERATIONS_CAPABILITY in sync_configuration(configured).capabilities
    without = Settings.model_construct(**(dict.fromkeys(_CAPABILITY_FLAGS, True) | {flag: False}))
    assert TEXT_OPERATIONS_CAPABILITY not in sync_configuration(without).capabilities


def test_the_switch_is_off_by_default_and_the_capability_optional(monkeypatch):
    monkeypatch.delenv("SYNC_PUBLICATION_TEXT_OPERATIONS_ENABLED", raising=False)
    assert Settings(_env_file=None).sync_publication_text_operations_enabled is False
    assert TEXT_OPERATIONS_CAPABILITY not in REQUIRED_SYNC_CAPABILITIES


def _names_and_modules(relative: str) -> tuple[set[str], set[str]]:
    tree = ast.parse((SRC / relative).read_text())
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return names, modules


def test_the_text_service_creates_no_publication_and_touches_no_paid_right():
    names, modules = _names_and_modules("services/public_mcp_text.py")
    forbidden = {
        "create_sync_target",
        "Resource",
        "Context",
        "paid_integration_write",
        "require_paid_integration",
        "inspect_paid_integration",
        "activate_installation",
    }
    assert names & forbidden == set()
    assert modules & {"services.integration_entitlements", "api.routes.public_mcp_endpoints"} == (
        set()
    )
    # The application's command never passes a name: the public name stays the owner's.
    tree = ast.parse((SRC / "services/public_mcp_text.py").read_text())
    command = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "update_installation_public_text"
    )
    calls = [
        node
        for node in ast.walk(command)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "apply_public_text"
    ]
    assert len(calls) == 1
    assert {kw.arg for kw in calls[0].keywords} == {"actor_user_id", "description", "usage_guide"}


def test_the_route_is_experimental_and_documented(app):
    operation = app.openapi()["paths"][
        "/api/v1/workspaces/{workspace_id}/sync-installations/{installation_id}/publication-text"
    ]["post"]
    assert "experimental" in operation["description"].lower()
    assert operation["requestBody"]["required"] is True
    assert operation["tags"] == ["workspace-sync"]


def test_the_recorded_client_id_fits_the_operation_record():
    from models.operation_history import OperationHistory
    from services.public_mcp_text import history_client_id

    width = OperationHistory.__table__.c.client_id.type.length  # type: ignore[attr-defined]
    assert width == 48
    short = "browser-client.example"
    assert history_client_id(short) == short
    assert history_client_id("c" * 48) == "c" * 48
    long_ids = ["c" * 49, "c" * 128, "d" * 128]
    stored = [history_client_id(value) for value in long_ids]
    assert all(len(value) == 48 and value.startswith("sha256:") for value in stored)
    assert len(set(stored)) == 3
    assert history_client_id("c" * 128) == stored[1]
