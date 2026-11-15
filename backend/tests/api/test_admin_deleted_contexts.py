"""``GET /api/v1/admin/contexts/deleted`` lists what the context restore can restore (#2833).

The gate, the query parsing, the selected columns and the response model,
without a database. The rows themselves are checked against Postgres in
``tests/integration/test_admin_deleted_contexts_db.py``.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from api.main import app
from api.routes.admin import DeletedContextItem, DeletedContextListResponse
from auth.dependencies import get_current_user_optional
from config.settings import get_settings
from db.base import get_db
from tests.api.test_capacity_lock_route_table import (
    NOT_APPLICABLE,
    ROUTER_DEFAULTS,
    _registered_routes,
)
from tests.api.test_system_admin_surface import ADMIN_SURFACE, OWN_ACCOUNT_ROUTES

ROUTE = ("GET", "/api/v1/admin/contexts/deleted")
URL = ROUTE[1]
SNAPSHOT = Path(__file__).parent / "fixtures" / "openapi_schema_snapshot.json"
ITEM_FIELDS = {
    "context_id",
    "name",
    "workspace_id",
    "workspace_name",
    "created_by",
    "deleted_at",
    "deleted_by",
    "deleted_memories",
    "name_taken",
    "resource_taken",
}


def _sql(statement) -> str:
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    return " ".join(str(compiled).split())


class _Empty:
    def scalar_one(self) -> int:
        return 0

    def scalar(self) -> int:
        return 0

    def all(self) -> list:
        return []


@pytest.fixture
def session() -> MagicMock:
    """A session that answers every statement with no row and keeps the SQL."""
    db = MagicMock()
    db.statements = []

    async def _execute(statement, *args, **kwargs):
        db.statements.append(_sql(statement))
        return _Empty()

    db.execute = AsyncMock(side_effect=_execute)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture
def client(session):
    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def session_admin(monkeypatch) -> dict:
    monkeypatch.setattr(get_settings(), "system_admin_mode", "session")

    async def _admin():
        return {"user_id": "1001", "email": "admin@example.com", "role": "admin"}

    app.dependency_overrides[get_current_user_optional] = _admin
    return {}


@pytest.fixture
def operator(monkeypatch) -> dict:
    token = secrets.token_hex(32)
    settings = get_settings()
    monkeypatch.setattr(settings, "system_admin_mode", "service")
    monkeypatch.setattr(settings, "system_admin_service_token", token)
    monkeypatch.setattr(settings, "system_admin_service_token_previous", "")
    monkeypatch.setattr(settings, "system_admin_service_allowed_cidrs", "")
    return {"Authorization": f"Bearer {token}", "X-Operator-Email": "ops@example.com"}


def test_the_route_is_in_the_admin_surface_and_not_an_own_account_route():
    assert ROUTE in ADMIN_SURFACE
    assert ROUTE not in OWN_ACCOUNT_ROUTES


def test_the_route_is_classified_in_the_capacity_lock_route_table():
    module = _registered_routes()[ROUTE]
    assert module == "admin"
    assert ROUTER_DEFAULTS[module] == NOT_APPLICABLE


@pytest.mark.parametrize("mode", ["session", "service"])
def test_the_route_answers_a_session_admin_and_a_service_operator(client, session, mode, request):
    headers = request.getfixturevalue("session_admin" if mode == "session" else "operator")
    response = client.get(URL, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
    # No row on the page: the count and the page only, no lookup.
    assert len(session.statements) == 2
    # Read only.
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


def test_the_route_is_404_in_off_mode(client, session, monkeypatch):
    monkeypatch.setattr(get_settings(), "system_admin_mode", "off")

    async def _admin():
        return {"user_id": "1001", "email": "admin@example.com", "role": "admin"}

    app.dependency_overrides[get_current_user_optional] = _admin
    response = client.get(URL)
    assert response.status_code == 404
    assert session.statements == []


def test_a_normal_user_is_refused(client, session, monkeypatch):
    monkeypatch.setattr(get_settings(), "system_admin_mode", "session")

    async def _user():
        return {"user_id": "1002", "email": "user@example.com", "role": "user"}

    app.dependency_overrides[get_current_user_optional] = _user
    response = client.get(URL)
    assert response.status_code == 403
    assert session.statements == []


@pytest.mark.parametrize(
    "params",
    [
        {"workspace_id": "not-a-uuid"},
        {"limit": "0"},
        {"limit": "501"},
        {"offset": "-1"},
        {"created_by": ""},
        {"deleted_by": ""},
        {"created_by": "x" * 256},
        {"deleted_by": "x" * 256},
    ],
)
def test_a_bad_value_is_a_422_and_is_not_clamped(client, session, session_admin, params):
    response = client.get(URL, params=params)
    assert response.status_code == 422, response.text
    assert session.statements == []


def test_the_queries_select_named_columns_and_never_the_users_table(client, session, session_admin):
    workspace_id = "00000000-0000-4000-8000-000000000001"
    response = client.get(
        URL, params={"workspace_id": workspace_id, "created_by": "u-1", "deleted_by": "u-2"}
    )
    assert response.status_code == 200, response.text
    count, page = session.statements
    for statement in (count, page):
        assert "contexts.deleted_at IS NOT NULL" in statement
        assert "workspaces.deleted_at IS NULL" in statement
        assert f"contexts.workspace_id = '{workspace_id}'" in statement
        assert "contexts.created_by = 'u-1'" in statement
        assert "contexts.deleted_by = 'u-2'" in statement
        assert "users" not in statement
    assert count.startswith("SELECT count(*)")
    assert "ORDER BY contexts.deleted_at DESC, contexts.id" in page
    assert page.endswith("LIMIT 50 OFFSET 0")
    selected = page.split(" FROM ")[0]
    for column in (
        "display_name",
        "description",
        "summary",
        "usage_guide",
        "resource_id",
        "is_private",
        "is_public",
        "trust_tier",
    ):
        assert f"contexts.{column}" not in selected, column


def test_the_item_model_has_the_ten_named_fields_and_forbids_others():
    assert set(DeletedContextItem.model_fields) == ITEM_FIELDS
    assert DeletedContextItem.model_config.get("extra") == "forbid"
    assert DeletedContextItem.model_config.get("from_attributes") is not True
    assert set(DeletedContextListResponse.model_fields) == {"items", "total", "limit", "offset"}
    assert DeletedContextListResponse.model_config.get("extra") == "forbid"


def test_no_filter_value_is_logged(client, session, session_admin):
    marker = f"uid-{secrets.token_hex(8)}"
    with structlog.testing.capture_logs() as events:
        response = client.get(URL, params={"created_by": marker, "deleted_by": marker})
    assert response.status_code == 200
    assert marker not in json.dumps(events, default=str)


def test_the_openapi_snapshot_lists_the_route():
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    operation = document["paths"][URL]["get"]
    assert {parameter["name"] for parameter in operation["parameters"]} == {
        "workspace_id",
        "created_by",
        "deleted_by",
        "limit",
        "offset",
    }
    schemas = document["components"]["schemas"]
    assert set(schemas["DeletedContextItem"]["properties"]) == ITEM_FIELDS
    assert schemas["DeletedContextItem"]["additionalProperties"] is False
