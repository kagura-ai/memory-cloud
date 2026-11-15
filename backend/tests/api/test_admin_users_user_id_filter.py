"""``GET /api/v1/admin/users?user_id=`` finds one account by its exact id (#2843).

The filter is ``users.user_id = :value``: exact, case-sensitive, nothing
trimmed, ANDed with the other filters, and a plain column of ``users`` (no
join, so ``total`` is unaffected). The rows themselves are checked against
Postgres in ``tests/integration/test_admin_users_user_id_filter_db.py``.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from api.main import app
from api.routes.admin import _apply_user_list_filters
from auth.dependencies import get_current_user_optional
from config.settings import get_settings
from db.base import get_db
from models.auth import User

SNAPSHOT = Path(__file__).parent / "fixtures" / "openapi_schema_snapshot.json"


def _sql(statement) -> str:
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    return " ".join(str(compiled).split())


def _filtered(**filters) -> tuple[str, bool]:
    defaults = {"search": None, "role": None, "workspace_id": None, "plan": None}
    statement, joined = _apply_user_list_filters(select(User.user_id), **{**defaults, **filters})
    return _sql(statement), joined


class _Empty:
    def scalar(self) -> int:
        return 0

    def scalars(self) -> _Empty:
        return self

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


def test_user_id_is_an_exact_match_on_the_users_column_without_a_join():
    sql, joined = _filtered(user_id="google-oauth2|Abc")
    assert sql.endswith("WHERE users.user_id = 'google-oauth2|Abc'")
    assert "JOIN" not in sql
    assert joined is False


def test_user_id_is_not_trimmed_folded_or_turned_into_a_pattern():
    sql, _ = _filtered(user_id=" Abc_ ")
    assert sql.endswith("WHERE users.user_id = ' Abc_ '")
    assert "LIKE" not in sql.upper()
    assert "lower(" not in sql


def test_user_id_is_anded_with_the_other_filters():
    sql, joined = _filtered(
        user_id="u-1", role="admin", admitted_via="allowlist", invited_by="u-2", search="ann"
    )
    assert joined is False
    for predicate in (
        "users.user_id = 'u-1'",
        "users.role = 'admin'",
        "users.admitted_via = 'allowlist'",
        "users.admitted_by_user_id = 'u-2'",
        "users.email ILIKE '%%ann%%'",
    ):
        assert predicate in sql, predicate
    assert " OR users.user_id" not in sql
    assert sql.count(" AND ") == 4


def test_without_user_id_the_statement_is_unchanged():
    assert _filtered() == _filtered(user_id=None)
    sql, _ = _filtered(role="user")
    assert "users.user_id =" not in sql


@pytest.mark.parametrize("mode", ["session", "service"])
def test_the_route_filters_the_count_and_the_rows_in_session_and_service_mode(
    client, session, mode, request
):
    headers = request.getfixturevalue("session_admin" if mode == "session" else "operator")
    response = client.get("/api/v1/admin/users", params={"user_id": "No-Such-Id"}, headers=headers)
    assert response.status_code == 200, response.text
    # An unknown id is an empty list, not a 404.
    assert response.json() == {"users": [], "total": 0}
    # The count and the select only: nothing else is read for an unknown id.
    count, rows = session.statements
    assert count.startswith("SELECT count(*)")
    assert "users.user_id = 'No-Such-Id'" in count
    assert "users.user_id = 'No-Such-Id'" in rows


def test_without_the_parameter_the_route_filters_nothing(client, session, session_admin):
    response = client.get("/api/v1/admin/users")
    assert response.status_code == 200, response.text
    assert response.json() == {"users": [], "total": 0}
    assert all("users.user_id =" not in statement for statement in session.statements)


@pytest.mark.parametrize("value", ["", "x" * 256])
def test_an_empty_or_over_long_user_id_is_a_422(client, session, session_admin, value):
    response = client.get("/api/v1/admin/users", params={"user_id": value})
    assert response.status_code == 422, response.text
    assert session.statements == []


def test_a_255_character_user_id_is_accepted(client, session, session_admin):
    response = client.get("/api/v1/admin/users", params={"user_id": "x" * 255})
    assert response.status_code == 200, response.text


def test_the_filter_value_is_not_logged(client, session, session_admin):
    marker = f"uid-{secrets.token_hex(8)}"
    with structlog.testing.capture_logs() as events:
        response = client.get("/api/v1/admin/users", params={"user_id": marker})
    assert response.status_code == 200
    assert marker not in json.dumps(events, default=str)


def test_the_openapi_snapshot_lists_the_user_id_parameter():
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    parameters = {
        parameter["name"]: parameter
        for parameter in document["paths"]["/api/v1/admin/users"]["get"]["parameters"]
    }
    assert parameters["user_id"]["in"] == "query"
    assert parameters["user_id"]["required"] is False
    constraints = json.dumps(parameters["user_id"]["schema"])
    assert '"minLength": 1' in constraints
    assert '"maxLength": 255' in constraints
    # The answer has no new field: the list still returns UserListResponse.
    assert parameters.keys() >= {"search", "role", "admitted_via", "invited_by", "user_id"}
