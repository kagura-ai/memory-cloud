"""``GET /api/v1/admin/embedding/failures`` says where failed embeddings are (#2832).

The gate, the query parsing, the statements and the response model, without
a database. The counts are checked against Postgres, the admin retry and the
sweep's diagnostic in ``tests/integration/test_admin_embedding_failures_db.py``.
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
from api.routes.admin import EmbeddingFailureGroup, EmbeddingFailureListResponse
from auth.dependencies import get_current_user_optional
from config.settings import get_settings
from db.base import get_db
from tests.api.test_capacity_lock_route_table import (
    NOT_APPLICABLE,
    ROUTER_DEFAULTS,
    _registered_routes,
)
from tests.api.test_system_admin_surface import ADMIN_SURFACE, OWN_ACCOUNT_ROUTES

ROUTE = ("GET", "/api/v1/admin/embedding/failures")
URL = ROUTE[1]
SNAPSHOT = Path(__file__).parent / "fixtures" / "openapi_schema_snapshot.json"
GROUP_FIELDS = {
    "workspace_id",
    "workspace_name",
    "context_id",
    "context_name",
    "failed",
    "stalled",
    "capped",
    "configuration",
    "other",
    "erased",
    "oldest_attempt_at",
    "newest_attempt_at",
}
WORKSPACE = UUID("00000000-0000-4000-8000-000000000001")
CONTEXT = UUID("00000000-0000-4000-8000-000000000002")


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
    assert response.json() == {"total": 0, "limit": 100, "offset": 0, "groups": []}
    # No group on the page: the count and the page only, no name lookup.
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
        {"workspace_id": "not-a-uuid"},
        {"context_id": "not-a-uuid"},
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


def test_the_statements_count_the_rows_the_retry_counts_grouped_and_ordered(serve, session_admin):
    session = _session()
    response = serve(session).get(
        URL, params={"workspace_id": str(WORKSPACE), "context_id": str(CONTEXT), "limit": "7"}
    )
    assert response.status_code == 200, response.text
    count, page = session.statements
    for statement in (count, page):
        assert "memories.embedding_status = 'failed'" in statement
        assert "memories.deleted_at IS NULL" in statement
        assert f"memories.workspace_id = '{WORKSPACE}'" in statement
        assert f"memories.context_id = '{CONTEXT}'" in statement
        assert "GROUP BY memories.workspace_id, memories.context_id" in statement
        # Aggregates only: no column that identifies a memory or its author.
        for column in ("id", "summary", "content", "tags", "user_id", "details"):
            assert f"memories.{column}" not in statement.replace("memories.context_id", ""), column
    assert count.startswith("SELECT count(*)")
    assert "LIKE 'QUOTA-002:%%'" in page
    assert "LIKE 'CFG-%%'" in page and "LIKE 'FEAT-%%'" in page
    assert "memories.embedding_retry_count >= 3" in page
    assert "ORDER BY failed DESC, memories.workspace_id, memories.context_id" in page
    assert page.endswith("LIMIT 7 OFFSET 0")


def test_a_group_is_answered_with_its_names_classes_and_times(serve, session_admin):
    oldest, newest = datetime(2026, 11, 1, 3, 0, 0), datetime(2026, 11, 14, 3, 0, 0)
    session = _session(
        _Answer(count=1),
        _Answer(
            rows=[
                SimpleNamespace(
                    workspace_id=WORKSPACE,
                    context_id=CONTEXT,
                    failed=12,
                    stalled=3,
                    capped=1,
                    configuration=7,
                    erased=2,
                    oldest_attempt_at=oldest,
                    newest_attempt_at=newest,
                )
            ]
        ),
        _Answer(rows=[SimpleNamespace(id=WORKSPACE, name="Acme")]),
        _Answer(rows=[SimpleNamespace(id=CONTEXT, name="notes")]),
    )
    response = serve(session).get(URL)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "total": 1,
        "limit": 100,
        "offset": 0,
        "groups": [
            {
                "workspace_id": str(WORKSPACE),
                "workspace_name": "Acme",
                "context_id": str(CONTEXT),
                "context_name": "notes",
                "failed": 12,
                "stalled": 3,
                "capped": 1,
                "configuration": 7,
                "other": 4,
                "erased": 2,
                "oldest_attempt_at": "2026-11-01T03:00:00Z",
                "newest_attempt_at": "2026-11-14T03:00:00Z",
            }
        ],
    }
    # The name lookups bind the page's ids only.
    _, _, workspaces, contexts = session.statements
    assert workspaces.endswith(f"WHERE workspaces.id IN ('{WORKSPACE}')")
    assert contexts.endswith(f"WHERE contexts.id IN ('{CONTEXT}')")


def test_names_are_null_when_the_workspace_or_the_context_has_no_row(serve, session_admin):
    session = _session(
        _Answer(count=1),
        _Answer(
            rows=[
                SimpleNamespace(
                    workspace_id=WORKSPACE,
                    context_id=CONTEXT,
                    failed=1,
                    stalled=0,
                    capped=0,
                    configuration=0,
                    erased=0,
                    oldest_attempt_at=None,
                    newest_attempt_at=None,
                )
            ]
        ),
    )
    response = serve(session).get(URL)
    assert response.status_code == 200, response.text
    [group] = response.json()["groups"]
    assert group["workspace_name"] is None
    assert group["context_name"] is None
    assert group["oldest_attempt_at"] is None
    assert group["newest_attempt_at"] is None
    assert group["other"] == 1


def test_a_group_without_a_context_looks_up_no_context(serve, session_admin):
    session = _session(
        _Answer(count=1),
        _Answer(
            rows=[
                SimpleNamespace(
                    workspace_id=WORKSPACE,
                    context_id=None,
                    failed=2,
                    stalled=2,
                    capped=0,
                    configuration=0,
                    erased=0,
                    oldest_attempt_at=None,
                    newest_attempt_at=None,
                )
            ]
        ),
        _Answer(rows=[SimpleNamespace(id=WORKSPACE, name="Acme")]),
    )
    response = serve(session).get(URL)
    assert response.status_code == 200, response.text
    [group] = response.json()["groups"]
    assert (group["context_id"], group["context_name"]) == (None, None)
    assert group["workspace_name"] == "Acme"
    # The count, the page and the workspace names: no context lookup.
    assert len(session.statements) == 3


def test_the_group_model_has_the_twelve_named_fields_and_forbids_others():
    assert set(EmbeddingFailureGroup.model_fields) == GROUP_FIELDS
    assert EmbeddingFailureGroup.model_config.get("extra") == "forbid"
    assert EmbeddingFailureGroup.model_config.get("from_attributes") is not True
    assert set(EmbeddingFailureListResponse.model_fields) == {"total", "limit", "offset", "groups"}
    assert EmbeddingFailureListResponse.model_config.get("extra") == "forbid"


def test_the_log_line_holds_counts_and_no_id(serve, session_admin):
    with structlog.testing.capture_logs() as events:
        response = serve(_session()).get(
            URL, params={"workspace_id": str(WORKSPACE), "context_id": str(CONTEXT)}
        )
    assert response.status_code == 200
    logged = json.dumps(events, default=str)
    assert str(WORKSPACE) not in logged
    assert str(CONTEXT) not in logged
    [line] = [event for event in events if event["event"] == "admin_embedding_failures_listed"]
    assert line["workspace_filter"] is True and line["context_filter"] is True


def test_the_openapi_snapshot_lists_the_route():
    document = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    operation = document["paths"][URL]["get"]
    assert {parameter["name"] for parameter in operation["parameters"]} == {
        "workspace_id",
        "context_id",
        "limit",
        "offset",
    }
    schemas = document["components"]["schemas"]
    assert set(schemas["EmbeddingFailureGroup"]["properties"]) == GROUP_FIELDS
    assert schemas["EmbeddingFailureGroup"]["additionalProperties"] is False
