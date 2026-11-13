"""GET /api/v1/admin/operations/status: gate, headers, single flight (#2768).

The document itself is built from the database in
``tests/services/test_operations_status_db.py``; here the reader is a stub so
the gate is tested in every ``SYSTEM_ADMIN_MODE`` without Postgres.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routes import admin_operations
from auth.dependencies import get_current_user_optional
from auth.system_admin_mode import routes_behind_require_admin
from config.settings import get_settings
from db.base import get_db
from services.operations.status import OperationsStatusV1

URL = "/api/v1/admin/operations/status"
ADMIN_SESSION = {"user_id": "1001", "email": "admin@example.com", "role": "admin"}
USER_SESSION = {"user_id": "1002", "email": "user@example.com", "role": "user"}
OPERATOR = "ops@example.com"

DOCUMENT = OperationsStatusV1(
    as_of=datetime(2026, 11, 13, 12, 0, tzinfo=UTC),
    overdue_grace_seconds=900,
    scheduler_registered_at=None,
    probe_enabled=False,
    jobs=[],
    backlogs=[],
    llm=[],
)


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []

    async def _read(db, *, now, settings):
        calls.append(1)
        return DOCUMENT

    monkeypatch.setattr(admin_operations, "read_operations_status", _read)
    admin_operations._single_flight.clear()
    yield calls
    admin_operations._single_flight.clear()


@pytest.fixture
def mode(monkeypatch: pytest.MonkeyPatch):
    def _apply(value: str, **values: str) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "system_admin_mode", value)
        defaults = {
            "system_admin_service_token": "",
            "system_admin_service_token_previous": "",
            "system_admin_service_allowed_cidrs": "",
        }
        defaults.update(values)
        for field, item in defaults.items():
            monkeypatch.setattr(settings, field, item)

    return _apply


@pytest.fixture
def client():
    async def _db():
        yield object()

    app.dependency_overrides[get_db] = _db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _as(principal: dict | None) -> None:
    if principal is None:
        app.dependency_overrides.pop(get_current_user_optional, None)
        return

    async def _session():
        return dict(principal)

    app.dependency_overrides[get_current_user_optional] = _session


def test_session_admin_gets_the_document_uncached(client, mode, reads) -> None:
    mode("session")
    _as(ADMIN_SESSION)
    response = client.get(URL)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["schema_version"] == 1
    assert body["as_of"] == "2026-11-13T12:00:00Z"
    assert set(body) == {
        "schema_version",
        "as_of",
        "overdue_grace_seconds",
        "scheduler_registered_at",
        "probe_enabled",
        "jobs",
        "backlogs",
        "llm",
    }


def test_session_refusals(client, mode, reads) -> None:
    mode("session")
    _as(None)
    assert client.get(URL).status_code == 401
    _as(USER_SESSION)
    assert client.get(URL).status_code == 403
    assert reads == []


def test_off_is_an_unknown_path_for_an_admin(client, mode, reads) -> None:
    mode("off")
    _as(ADMIN_SESSION)
    response = client.get(URL)
    assert response.status_code == 404
    assert response.json() == {"error": "HTTP-404", "message": "Not Found", "details": {}}
    assert reads == []


def test_service_operator_gets_the_document(client, mode, reads) -> None:
    token = secrets.token_hex(32)
    mode("service", system_admin_service_token=token)
    _as(None)
    response = client.get(
        URL, headers={"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR}
    )
    assert response.status_code == 200, response.text
    assert response.json()["schema_version"] == 1
    # Without the credential: refused, nothing read.
    assert client.get(URL, headers={"X-Operator-Email": OPERATOR}).status_code == 401
    assert len(reads) == 1


def test_rapid_polls_share_one_read(client, mode, reads) -> None:
    mode("session")
    _as(ADMIN_SESSION)
    for _ in range(3):
        assert client.get(URL).status_code == 200
    assert len(reads) == 1


def test_the_route_is_in_the_openapi_document_with_its_tag(mode) -> None:
    mode("session")
    app.openapi_schema = None
    try:
        operation = app.openapi()["paths"][URL]["get"]
    finally:
        app.openapi_schema = None
    assert operation["tags"] == ["admin-operations"]
    tags = {tag["name"] for tag in app.openapi_tags or []}
    assert "admin-operations" in tags


def test_every_admin_path_is_behind_the_admin_gate() -> None:
    """A route under /api/v1/admin/ must follow SYSTEM_ADMIN_MODE (require_admin)."""
    gated = {
        (method, context.path)
        for context in routes_behind_require_admin(app)
        for method in context.methods - {"HEAD", "OPTIONS"}
    }
    schema_paths = app.openapi()["paths"]
    admin_operations_in_schema = {
        (method.upper(), path)
        for path, item in schema_paths.items()
        if path.startswith("/api/v1/admin/")
        for method in item
        if method in {"get", "put", "post", "delete", "patch"}
    }
    assert admin_operations_in_schema
    assert admin_operations_in_schema <= gated, admin_operations_in_schema - gated
    assert ("GET", URL) in gated
