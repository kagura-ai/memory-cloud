"""One ``Authorization`` header on every service-token route (#2801).

The billing and worker service tokens used to read only the first
``Authorization`` header, so a request carrying two was judged on whichever a
proxy put first. Every route that reaches ``verify_billing_service_token``,
``verify_worker_token`` or ``billing_integration_caller`` now treats more than
one header exactly like a missing one (the same 401), before comparing
anything and before the database is opened. An unset token still answers 503
first.

The routes are discovered from ``api.main.app`` and pinned, so a new route on
these gates shows up here; the requests go to a minimal app built from the
three routers, with ``get_db`` replaced by a recorder.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient

from api.main import app as main_app
from api.main import memory_cloud_exception_handler
from api.routes import internal_billing, internal_integrations, workers
from auth.single_authorization import sole_authorization
from config.settings import get_settings
from db.base import get_db
from utils.exceptions import MemoryCloudException

BILLING_TOKEN = "billing-" + "a" * 40
WORKER_TOKEN = "worker-" + "b" * 40
SAMPLE_ID = "00000000-0000-4000-8000-000000000001"

BILLING_GATES = (
    internal_billing.verify_billing_service_token,
    internal_integrations.billing_integration_caller,
)
WORKER_GATES = (workers.verify_worker_token,)

EXPECTED = {
    ("PUT", "/internal/workspaces/{workspace_id}/plan"): BILLING_TOKEN,
    ("GET", "/internal/workspaces/{workspace_id}/plan"): BILLING_TOKEN,
    ("GET", "/internal/workspaces/{workspace_id}/owner"): BILLING_TOKEN,
    ("GET", "/internal/workspaces/{workspace_id}/downgrade-eligibility"): BILLING_TOKEN,
    ("POST", "/internal/workspaces/{workspace_id}/capacity-lock-notice"): BILLING_TOKEN,
    ("GET", "/internal/workspaces/{workspace_id}/integration-entitlements"): BILLING_TOKEN,
    ("PUT", "/internal/workspaces/{workspace_id}/integration-entitlements"): BILLING_TOKEN,
    ("GET", "/api/v1/workers/apps"): WORKER_TOKEN,
    ("GET", "/api/v1/workers/config"): WORKER_TOKEN,
    ("GET", "/api/v1/workers/active-color"): WORKER_TOKEN,
}


def _reaches(dependant, gates) -> bool:
    return any(dep.call in gates or _reaches(dep, gates) for dep in dependant.dependencies)


def _discovered() -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for context in iter_route_contexts(main_app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        billing = _reaches(context.dependant, BILLING_GATES)
        worker = _reaches(context.dependant, WORKER_GATES)
        if not (billing or worker):
            continue
        assert not (billing and worker), context.path
        for method in context.methods - {"HEAD", "OPTIONS"}:
            found[(method, context.path)] = BILLING_TOKEN if billing else WORKER_TOKEN
    return found


ROUTES = sorted(EXPECTED)
ROUTE_IDS = [f"{method} {path}" for method, path in ROUTES]


def test_the_service_token_routes_are_exactly_the_pinned_ten():
    discovered = _discovered()
    assert discovered == EXPECTED
    tokens = list(discovered.values())
    assert (tokens.count(BILLING_TOKEN), tokens.count(WORKER_TOKEN)) == (7, 3)


class _Reached(Exception):
    """Raised by the recorders: the request got past the credential check."""


@pytest.fixture
def client(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "billing_service_token", BILLING_TOKEN)
    monkeypatch.setattr(settings, "worker_service_token", WORKER_TOKEN)
    opened: list[str] = []

    async def recording_db():
        opened.append("db")
        raise _Reached()
        yield  # pragma: no cover - makes this a generator dependency

    async def recording_marker(*_args, **_kwargs):
        opened.append("marker")
        raise _Reached()

    monkeypatch.setattr(workers, "read_active_color_settled", recording_marker)
    app = FastAPI()
    app.include_router(workers.router, prefix="/api/v1")
    app.include_router(internal_billing.router)
    app.include_router(internal_integrations.router)
    app.add_exception_handler(MemoryCloudException, memory_cloud_exception_handler)

    @app.exception_handler(_Reached)
    async def reached(_request, _exc):
        return JSONResponse({"reached": True}, status_code=418)

    app.dependency_overrides[get_db] = recording_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        test_client.opened = opened  # type: ignore[attr-defined]
        yield test_client


def _call(client, method, path, headers):
    return client.request(
        method,
        path.replace("{workspace_id}", SAMPLE_ID),
        params={"platform": "slack", "team_id": "T1"},
        json=None if method == "GET" else {},
        headers=headers,
    )


@pytest.mark.parametrize(("method", "path"), ROUTES, ids=ROUTE_IDS)
@pytest.mark.parametrize("shape", ["valid_first", "valid_second", "both_valid"])
def test_two_authorization_headers_are_401_before_the_database(client, method, path, shape):
    token = EXPECTED[(method, path)]
    valid, other = f"Bearer {token}", "Bearer " + "c" * 40
    pair = {
        "valid_first": [valid, other],
        "valid_second": [other, valid],
        "both_valid": [valid, valid],
    }[shape]
    response = _call(client, method, path, [("Authorization", value) for value in pair])
    assert response.status_code == 401, response.text
    assert client.opened == []


@pytest.mark.parametrize(("method", "path"), ROUTES, ids=ROUTE_IDS)
def test_one_valid_header_gets_past_the_credential(client, method, path):
    token = EXPECTED[(method, path)]
    response = _call(client, method, path, {"Authorization": f"Bearer {token}"})
    assert response.status_code == 418, response.text
    assert len(client.opened) == 1


@pytest.mark.parametrize(("method", "path"), ROUTES, ids=ROUTE_IDS)
def test_a_missing_header_is_401(client, method, path):
    response = _call(client, method, path, {})
    assert response.status_code == 401, response.text
    assert client.opened == []


@pytest.mark.parametrize(("method", "path"), ROUTES, ids=ROUTE_IDS)
@pytest.mark.parametrize("count", [0, 1, 2])
def test_an_unset_token_is_503_whatever_the_headers(client, monkeypatch, method, path, count):
    # Also for two headers on the integration-entitlements routes, which
    # answered 401 before the shared check (#2801).
    token = EXPECTED[(method, path)]
    name = "billing_service_token" if token == BILLING_TOKEN else "worker_service_token"
    monkeypatch.setattr(get_settings(), name, "")
    headers = [("Authorization", f"Bearer {token}")] * count
    response = _call(client, method, path, headers)
    assert response.status_code == 503, response.text
    assert client.opened == []


class _Headers:
    def __init__(self, values):
        self._values = values

    def getlist(self, name):
        assert name == "authorization"
        return list(self._values)


class _Request:
    def __init__(self, *values):
        self.headers = _Headers(values)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((), None),
        (("Bearer one",), "Bearer one"),
        (("Bearer one", "Bearer one"), None),
        (("Bearer one", "Bearer two"), None),
    ],
)
def test_sole_authorization_returns_only_a_single_value(values, expected):
    assert sole_authorization(_Request(*values)) == expected  # type: ignore[arg-type]


def test_sole_authorization_counts_every_spelling_of_the_header_name():
    # ASGI servers lower-case header names, so differently cased copies are
    # still two headers.
    app = FastAPI()

    @app.get("/probe")
    async def probe(request: Request):
        return {"sole": sole_authorization(request)}

    with TestClient(app) as test_client:
        two = test_client.get(
            "/probe", headers=[("Authorization", "Bearer one"), ("AUTHORIZATION", "Bearer one")]
        )
        one = test_client.get("/probe", headers={"AUTHORIZATION": "Bearer one"})
    assert two.json() == {"sole": None}
    assert one.json() == {"sole": "Bearer one"}
