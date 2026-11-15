"""``GET /api/v1/admin/force-transfer-requests`` lists the requests a second admin decides (#2846).

The gate, the query parsing, the statements and the response model, without
a database. The rows are checked against Postgres, the read by id and the
operations backlog count in
``tests/integration/test_admin_force_transfer_requests_list_db.py``.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
import structlog
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from api.main import app
from api.routes.admin import ForceTransferRequestItem, ForceTransferRequestListResponse
from auth.dependencies import get_current_user_optional
from config.settings import get_settings
from db.base import get_db
from tests.api.test_capacity_lock_route_table import (
    NOT_APPLICABLE,
    ROUTER_DEFAULTS,
    _registered_routes,
)
from tests.api.test_system_admin_surface import ADMIN_SURFACE, OWN_ACCOUNT_ROUTES

ROUTE = ("GET", "/api/v1/admin/force-transfer-requests")
URL = ROUTE[1]
SNAPSHOT = Path(__file__).parent / "fixtures" / "openapi_schema_snapshot.json"
# The eleven keys of GET /api/v1/admin/force-transfer-requests/{request_id}.
ITEM_FIELDS = {
    "request_id",
    "workspace_id",
    "target_user_id",
    "reason",
    "status",
    "initiated_by_user_id",
    "initiated_by_email",
    "ownership_epoch_at_initiation",
    "created_at",
    "decided_by_user_id",
    "decided_at",
}
STATUSES = ["pending", "approved", "cancelled", "superseded"]
REQUEST = UUID("00000000-0000-4000-8000-000000000001")
WORKSPACE = UUID("00000000-0000-4000-8000-000000000002")


def _sql(statement) -> str:
    compiled = statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )
    return " ".join(str(compiled).split())


class _Answer:
    def __init__(self, rows: list | None = None, count: int = 0) -> None:
        self._rows = rows or []
        self._count = count

    def scalar_one(self) -> int:
        return self._count

    def all(self) -> list:
        return list(self._rows)


def _session(*answers: _Answer) -> MagicMock:
    """A session that answers its statements in order (then with no row) and keeps the SQL."""
    db = MagicMock()
    db.statements = []
    queue = list(answers)

    async def _execute(statement, *args, **kwargs):
        db.statements.append(_sql(statement))
        return queue.pop(0) if queue else _Answer()

    db.execute = AsyncMock(side_effect=_execute)
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture
def serve():
    def _serve(session: MagicMock) -> TestClient:
        async def _db():
            yield session

        app.dependency_overrides[get_db] = _db
        return TestClient(app, raise_server_exceptions=False)

    yield _serve
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
def test_the_route_answers_a_session_admin_and_a_service_operator(serve, mode, request):
    headers = request.getfixturevalue("session_admin" if mode == "session" else "operator")
    session = _session()
    response = serve(session).get(URL, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}
    # The count and the page: the request table alone, nothing else is read.
    assert len(session.statements) == 2
    session.add.assert_not_called()
    session.commit.assert_not_awaited()


def test_the_route_is_404_in_off_mode(serve, monkeypatch):
    monkeypatch.setattr(get_settings(), "system_admin_mode", "off")

    async def _admin():
        return {"user_id": "1001", "email": "admin@example.com", "role": "admin"}

    app.dependency_overrides[get_current_user_optional] = _admin
    session = _session()
    client = serve(session)
    response = client.get(URL)
    assert response.status_code == 404
    assert response.json() == client.get("/api/v1/admin/__no_such_route__").json()
    assert session.statements == []


def test_a_normal_user_is_refused(serve, monkeypatch):
    monkeypatch.setattr(get_settings(), "system_admin_mode", "session")

    async def _user():
        return {"user_id": "1002", "email": "user@example.com", "role": "user"}

    app.dependency_overrides[get_current_user_optional] = _user
    session = _session()
    assert serve(session).get(URL).status_code == 403
    assert session.statements == []


@pytest.mark.parametrize(
    "params",
    [
        {"status": "open"},
        {"status": "PENDING"},
        {"status": ""},
        {"workspace_id": "not-a-uuid"},
        {"limit": "0"},
        {"limit": "501"},
        {"offset": "-1"},
    ],
)
def test_a_bad_value_is_a_422_and_is_not_clamped(serve, session_admin, params):
    session = _session()
    response = serve(session).get(URL, params=params)
    assert response.status_code == 422, response.text
    assert session.statements == []


@pytest.mark.parametrize("status", STATUSES)
def test_each_of_the_four_states_is_a_valid_filter(serve, session_admin, status):
    session = _session()
    response = serve(session).get(URL, params={"status": status})
    assert response.status_code == 200, response.text
    assert f"status = '{status}'" in session.statements[0]
    assert f"status = '{status}'" in session.statements[1]


def test_the_statements_read_the_request_table_alone_by_named_columns(serve, session_admin):
    session = _session()
    response = serve(session).get(
        URL, params={"status": "pending", "workspace_id": str(WORKSPACE), "limit": "9"}
    )
    assert response.status_code == 200, response.text
    table = "workspace_ownership_force_transfer_requests"
    count, page = session.statements
    for statement in (count, page):
        assert f"{table}.status = 'pending'" in statement
        assert f"{table}.workspace_id = '{WORKSPACE}'" in statement
        assert "JOIN" not in statement
        assert "users" not in statement
        assert "FROM workspaces" not in statement and ", workspaces" not in statement
        assert "decided_by_email" not in statement
    assert count.startswith("SELECT count(*)")
    assert f"ORDER BY {table}.created_at DESC, {table}.id" in page
    assert page.endswith("LIMIT 9 OFFSET 0")


def test_an_item_has_the_eleven_fields_of_the_read_by_id(serve, session_admin):
    row = SimpleNamespace(
        id=REQUEST,
        workspace_id=WORKSPACE,
        target_user_id="target-1",
        reason="Owner left the company",
        status="approved",
        initiated_by_user_id="operator:ann@example.com",
        initiated_by_email="operator:ann@example.com",
        ownership_epoch_at_initiation=4,
        created_at=datetime(2026, 11, 14, 3, 0, 0),
        decided_by_user_id="admin-2",
        decided_at=datetime(2026, 11, 14, 4, 30, 0),
    )
    session = _session(_Answer(count=1), _Answer(rows=[row]))
    response = serve(session).get(URL)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "items": [
            {
                "request_id": str(REQUEST),
                "workspace_id": str(WORKSPACE),
                "target_user_id": "target-1",
                "reason": "Owner left the company",
                "status": "approved",
                "initiated_by_user_id": "operator:ann@example.com",
                "initiated_by_email": "operator:ann@example.com",
                "ownership_epoch_at_initiation": 4,
                "created_at": "2026-11-14T03:00:00Z",
                "decided_by_user_id": "admin-2",
                "decided_at": "2026-11-14T04:30:00Z",
            }
        ],
        "total": 1,
        "limit": 50,
        "offset": 0,
    }


def test_the_item_model_has_the_eleven_named_fields_and_forbids_others():
    assert set(ForceTransferRequestItem.model_fields) == ITEM_FIELDS
    assert ForceTransferRequestItem.model_config.get("extra") == "forbid"
    assert ForceTransferRequestItem.model_config.get("from_attributes") is not True
    assert set(ForceTransferRequestListResponse.model_fields) == {
        "items",
        "total",
        "limit",
        "offset",
    }
    assert ForceTransferRequestListResponse.model_config.get("extra") == "forbid"


def test_the_status_filter_is_the_models_four_states():
    from typing import get_args

    from api.routes.admin import ForceTransferRequestStatus
    from models import auth as auth_models

    assert set(get_args(ForceTransferRequestStatus)) == {
        auth_models.FORCE_TRANSFER_STATUS_PENDING,
        auth_models.FORCE_TRANSFER_STATUS_APPROVED,
        auth_models.FORCE_TRANSFER_STATUS_CANCELLED,
        auth_models.FORCE_TRANSFER_STATUS_SUPERSEDED,
    }
    assert sorted(get_args(ForceTransferRequestStatus)) == sorted(STATUSES)


def test_the_log_line_holds_counts_and_no_filter_value(serve, session_admin):
    with structlog.testing.capture_logs() as events:
        response = serve(_session()).get(
            URL, params={"status": "pending", "workspace_id": str(WORKSPACE)}
        )
    assert response.status_code == 200
    assert str(WORKSPACE) not in json.dumps(events, default=str)
    [line] = [event for event in events if event["event"] == "admin_force_transfer_requests_listed"]
    assert line["status"] == "pending"
    assert line["workspace_filter"] is True


def test_the_openapi_snapshot_lists_the_route():
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    operation = document["paths"][URL]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
    assert set(parameters) == {"status", "workspace_id", "limit", "offset"}
    assert all(status in json.dumps(parameters["status"]["schema"]) for status in STATUSES)
    schemas = document["components"]["schemas"]
    assert set(schemas["ForceTransferRequestItem"]["properties"]) == ITEM_FIELDS
    assert schemas["ForceTransferRequestItem"]["additionalProperties"] is False
