"""The owner's custom-domain commands of an installation, without a database (#2921).

The switch and its prerequisites, the credential namespace and the request
bodies are refused before the feature runs a statement; the typed refusals
keep their headers; the command module creates no publication; the owner
API passes no guard to the lifecycle.
"""

import ast
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql

from api.routes import sync_installations
from api.routes.sync_installations import SyncControlRoute
from auth import oidc_limits
from auth.sync_identity import SyncPrincipal, sync_manager
from config.settings import get_settings
from db.base import get_db
from services.public_mcp_domains import PublicMcpDomainRateLimitError
from utils.datetime import utcnow

SWITCHES = (
    "oidc_enabled",
    "mcp_public_endpoints_enabled",
    "sync_integrations_enabled",
    "mcp_public_usage_budget_enabled",
    "mcp_public_domains_enabled",
    "sync_publication_domain_operations_enabled",
)
ACTIONS = ("claim", "verify", "remove")
SRC = Path(__file__).resolve().parents[2] / "src"


def _path(action: str) -> str:
    return f"/api/v1/workspaces/{uuid4()}/sync-installations/{uuid4()}/publication-domains/{action}"


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


def _body(action: str) -> dict:
    if action == "claim":
        return {"expected": _binding(), "hostname": "mcp.example.org"}
    return {"expected": _binding(), "domain": {"domain_id": str(uuid4()), "state": "pending"}}


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


async def _post(app, path: str, body: object, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        return await c.post(path, json=body, **kwargs)


@pytest.mark.parametrize("credential", [None, "Bearer kmc_oidc_at_" + "a" * 43])
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("switch", SWITCHES)
async def test_commands_are_404_before_authentication_while_the_capability_is_off(
    app, forbidden_db, monkeypatch, switch, action, credential
):
    monkeypatch.setattr(get_settings(), switch, False)
    response = await _post(
        app,
        _path(action),
        _body(action),
        headers={"Authorization": credential} if credential else {},
    )
    assert (response.status_code, response.json()) == (404, {"error": "not_found"})
    assert response.headers["cache-control"] == "no-store"
    assert forbidden_db == []


@pytest.mark.parametrize(
    "credential", [None, "Bearer kmc_rt_wrong", "Bearer ordinary-api-key", "Bearer session-token"]
)
@pytest.mark.parametrize("action", ACTIONS)
async def test_commands_refuse_other_credentials_before_the_database(
    app, forbidden_db, action, credential
):
    response = await _post(
        app,
        _path(action),
        _body(action),
        headers={"Authorization": credential} if credential else {},
        cookies={"session_token": "ignored-session"},
    )
    assert (response.status_code, response.json()) == (401, {"error": "invalid_token"})
    assert response.headers["cache-control"] == "no-store"
    assert forbidden_db == []


class _Empty:
    rowcount = 0

    def scalar(self):
        return None

    def scalar_one_or_none(self):
        return None

    def one_or_none(self):
        return None

    def first(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []


def _sql(statement) -> str:
    try:
        compiled = statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    except Exception:  # noqa: BLE001 - a statement that cannot render its values
        compiled = statement.compile(dialect=postgresql.dialect())
        return f"{compiled} {compiled.params!r}"
    return str(compiled)


@pytest.fixture
def session(app):
    """A session that answers every statement with no row and keeps the SQL."""
    db = MagicMock()
    db.statements = []

    async def _execute(statement, *args, **kwargs):
        db.statements.append(_sql(statement))
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

    async def manager() -> SyncPrincipal:
        return SyncPrincipal(
            "owner-2921",
            "browser-2921",
            "0" * 64,
            "integrations:manage",
            utcnow() + timedelta(minutes=5),
        )

    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[sync_manager] = manager
    return db


def _claim(**changes) -> dict:
    return {**_body("claim"), **changes}


def _command(**changes) -> dict:
    return {**_body("verify"), **changes}


def _without(body: dict, key: str) -> dict:
    return {**body, "expected": {k: v for k, v in body["expected"].items() if k != key}}


_STRICT_CASES = [
    ("claim", "", _claim(extra=1), 400),
    ("claim", "", {"hostname": "mcp.example.org"}, 400),
    ("claim", "", _without(_claim(), "generation"), 400),
    ("claim", "", _claim(domain={"domain_id": str(uuid4()), "state": "pending"}), 400),
    ("verify", "", _command(domain=None), 400),
    ("remove", "", _command(domain={"domain_id": str(uuid4()), "state": "released"}), 400),
    ("claim", "", {**_claim(), "expected": {**_binding(), "generation": "1"}}, 400),
    ("claim", "", _claim(hostname="a" * 256), 400),
    ("verify", "?domain_id=x", _command(), 400),
    ("claim", "", _claim(hostname="a" * 9000), 413),
]


@pytest.mark.parametrize(("action", "query", "body", "status"), _STRICT_CASES)
async def test_command_bodies_are_strict(app, session, action, query, body, status):
    response = await _post(app, _path(action) + query, body)
    expected = {"error": "request_too_large" if status == 413 else "invalid_request"}
    assert (response.status_code, response.json()) == (status, expected)
    assert response.headers["cache-control"] == "no-store"
    assert session.statements == []
    session.scalar.assert_not_called()
    session.get.assert_not_called()
    session.add.assert_not_called()


@pytest.mark.parametrize("action", ACTIONS)
async def test_a_body_that_is_not_json_is_refused(app, session, action):
    """The same JSON text with another content type: 400 before the first statement."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        response = await c.post(
            _path(action),
            content=json.dumps(_body(action)),
            headers={"content-type": "text/plain"},
        )
    assert (response.status_code, response.json()) == (400, {"error": "invalid_request"})
    assert response.headers["cache-control"] == "no-store"
    assert session.statements == []
    session.scalar.assert_not_called()
    session.get.assert_not_called()
    session.add.assert_not_called()


async def test_a_typed_refusal_keeps_its_response_headers():
    router = APIRouter(route_class=SyncControlRoute)

    @router.post("/limited")
    async def limited() -> None:
        raise PublicMcpDomainRateLimitError(retry_after=42, limit="verify_per_hour")

    application = FastAPI()
    application.include_router(router)
    response = await _post(application, "/limited", {})
    assert (response.status_code, response.json()) == (429, {"error": "RATE-001"})
    assert response.headers["retry-after"] == "42"
    assert response.headers["cache-control"] == "no-store"


def _tree(relative: str) -> ast.Module:
    return ast.parse((SRC / relative).read_text())


def test_the_command_module_creates_no_publication():
    tree = _tree("services/sync_publication_domain_commands.py")
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
    forbidden = {
        "create_sync_target",
        "PublicMcpEndpoint",
        "Resource",
        "Context",
        "paid_integration_write",
        "require_paid_integration",
        "activate_installation",
    }
    assert names & forbidden == set()
    assert (
        modules & {"services.integration_entitlements", "api.routes.public_mcp_endpoints"} == set()
    )


def _calls(tree: ast.Module, names: set[str]) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names
    ]


def test_the_owner_api_passes_no_guard():
    lifecycle = {"attach_domain", "verify_domain", "detach_domain"}
    calls = _calls(_tree("api/routes/public_mcp_domains.py"), lifecycle)
    assert sorted(call.func.id for call in calls) == sorted(lifecycle)  # type: ignore[attr-defined]
    assert all(kw.arg != "guard" for call in calls for kw in call.keywords)

    definitions: dict[str, ast.AsyncFunctionDef] = {}
    for node in _tree("services/public_mcp_domains.py").body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in lifecycle:
            definitions[node.name] = node  # the last one: the implementation
    assert set(definitions) == lifecycle
    for name, function in definitions.items():
        arguments = function.args
        assert [arg.arg for arg in arguments.kwonlyargs] == ["guard"], name
        default = arguments.kw_defaults[0]
        assert isinstance(default, ast.Constant) and default.value is None, name
        assert "guard" not in [arg.arg for arg in arguments.args], name


def test_the_three_routes_are_experimental(app):
    paths = app.openapi()["paths"]
    for action in ACTIONS:
        path = (
            "/api/v1/workspaces/{workspace_id}/sync-installations/{installation_id}"
            f"/publication-domains/{action}"
        )
        operation = paths[path]["post"]
        assert "experimental" in operation["description"].lower(), action
        assert operation["requestBody"]["required"] is True
