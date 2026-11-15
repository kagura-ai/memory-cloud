"""Admin sleep report reads: the report without its actions, and the actions in pages (#2842).

- ``GET /api/v1/admin/sleep-reports/{report_id}?include_actions=false`` answers
  ``actions: []`` with the run's ``action_count`` and reads no action row;
  without the parameter, and with ``true``, the answer is the one before.
- ``GET /api/v1/admin/sleep-reports/{report_id}/actions`` answers
  ``{actions, total, limit, offset}`` in ``id`` order; a ``limit`` outside
  1-500, a negative ``offset`` and a non-UUID id are a 422, an unknown report
  a 404 with the detail route's body.

The database is mocked; the walk over real rows is in
``tests/integration/test_admin_sleep_report_actions_db.py``.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from api.main import app
from auth.dependencies import get_current_user_optional, require_admin
from config.settings import get_settings
from db.base import get_db

ADMIN_SESSION = {"user_id": "1001", "email": "admin@example.com", "role": "admin"}
OPERATOR = "ops@example.com"
DETAILS_SENTINEL = f"details-sentinel-{secrets.token_hex(8)}"


def _report(*, context_id=None):
    r = MagicMock()
    r.id = uuid4()
    r.user_id = "local:target"
    r.workspace_id = None
    r.context_id = context_id
    r.status = "completed"
    r.source = "sleep"
    r.started_at = datetime(2026, 4, 6, 3, 0, 0)
    r.completed_at = datetime(2026, 4, 6, 3, 3, 0)
    r.memories_processed = 7
    r.edges_created = 2
    r.memories_merged = 1
    r.memories_promoted = 3
    r.memories_flagged = 0
    r.llm_calls_made = 4
    r.llm_tokens_used = 1200
    r.llm_call_failures = 0
    r.embedding_calls_made = 2
    r.error_message = None
    r.edge_discovery_result = None
    r.dedup_result = None
    r.importance_result = None
    r.consolidation_result = None
    r.reindex_result = None
    r.merge_retention_result = None
    return r


def _action(action_id: int):
    """One selected ``sleep_actions`` row (named columns)."""
    return SimpleNamespace(
        id=action_id,
        phase="dedup_merge",
        action_type="merge",
        memory_id=uuid4(),
        target_id=uuid4(),
        details={"loser_summary": DETAILS_SENTINEL},
        created_at=datetime(2026, 4, 6, 3, 1, 0),
    )


def _orm_action(action_id: int):
    a = MagicMock()
    for key, value in vars(_action(action_id)).items():
        setattr(a, key, value)
    return a


def _scalar_one_or_none(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _scalar(value):
    result = MagicMock()
    result.scalar.return_value = value
    return result


def _all(rows):
    result = MagicMock()
    result.all.return_value = list(rows)
    result.scalars.return_value.all.return_value = list(rows)
    return result


def _sql(mock_db, index: int) -> str:
    stmt = mock_db.execute.await_args_list[index].args[0]
    return str(stmt.compile(dialect=postgresql.dialect()))


def _params(mock_db, index: int) -> dict:
    stmt = mock_db.execute.await_args_list[index].args[0]
    return dict(stmt.compile(dialect=postgresql.dialect()).params)


def _install(mock_db, *, admin: bool = True) -> None:
    async def _db():
        yield mock_db

    app.dependency_overrides[get_db] = _db
    if admin:
        app.dependency_overrides[require_admin] = lambda: dict(ADMIN_SESSION)


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


@pytest.fixture
def gate(monkeypatch):
    """Set SYSTEM_ADMIN_MODE for the real ``require_admin``."""

    def _apply(mode: str, token: str = "") -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "system_admin_mode", mode)
        monkeypatch.setattr(settings, "system_admin_service_token", token)
        monkeypatch.setattr(settings, "system_admin_service_token_previous", "")
        monkeypatch.setattr(settings, "system_admin_service_allowed_cidrs", "")

    return _apply


class TestDetailWithoutActions:
    """``?include_actions=false``"""

    def test_include_actions_false_answers_no_actions_and_the_runs_count(self, client):
        report = _report()
        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _scalar_one_or_none(report),
            _scalar(4321),
            _all([("local:target", "target@example.com")]),
        ]
        _install(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}?include_actions=false")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["actions"] == []
        assert body["action_count"] == 4321
        assert body["report"]["id"] == str(report.id)
        assert body["report"]["memories_processed"] == 7
        assert body["report"]["user_email"] == "target@example.com"

    def test_include_actions_false_reads_no_action_row(self, client):
        """The only statement on ``sleep_actions`` is the count."""
        report = _report()
        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _scalar_one_or_none(report),
            _scalar(2),
            _all([]),
        ]
        _install(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}?include_actions=false")

        assert response.status_code == 200, response.text
        statements = [_sql(mock_db, i) for i in range(mock_db.execute.await_count)]
        on_actions = [sql for sql in statements if "sleep_actions" in sql]
        assert len(on_actions) == 1
        assert on_actions[0].lower().startswith("select count(")
        assert "sleep_actions.details" not in on_actions[0]
        assert "sleep_actions.id" not in on_actions[0]

    @pytest.mark.parametrize("query", ["", "?include_actions=true"])
    def test_default_and_true_answer_every_action_as_before(self, client, query):
        report = _report()
        actions = [_orm_action(1), _orm_action(2), _orm_action(3)]
        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _scalar_one_or_none(report),
            _all(actions),
            _all([]),
        ]
        _install(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report.id}{query}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert [a["id"] for a in body["actions"]] == [1, 2, 3]
        assert body["action_count"] == 3
        assert body["actions"][0]["details"] == {"loser_summary": DETAILS_SENTINEL}
        assert set(body["actions"][0]) == {
            "id",
            "phase",
            "action_type",
            "memory_id",
            "target_id",
            "details",
            "created_at",
        }
        # The unpaged read of the run's actions, ordered by id, and no count.
        actions_sql = _sql(mock_db, 1)
        assert "ORDER BY sleep_actions.id" in actions_sql
        assert "LIMIT" not in actions_sql
        assert mock_db.execute.await_count == 3

    def test_include_actions_false_unknown_report_is_404(self, client):
        mock_db = AsyncMock()
        mock_db.execute.side_effect = [_scalar_one_or_none(None)]
        _install(mock_db)
        report_id = uuid4()

        response = client.get(f"/api/v1/admin/sleep-reports/{report_id}?include_actions=false")

        assert response.status_code == 404
        assert response.json() == {
            "error": "HTTP-404",
            "message": f"Sleep report {report_id} not found.",
            "details": {},
        }
        assert mock_db.execute.await_count == 1

    def test_include_actions_must_be_a_boolean(self, client):
        mock_db = AsyncMock()
        _install(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{uuid4()}?include_actions=maybe")

        assert response.status_code == 422
        mock_db.execute.assert_not_awaited()


class TestActionsPage:
    """``GET /api/v1/admin/sleep-reports/{report_id}/actions``"""

    def _db(self, report_id, rows, total):
        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _scalar_one_or_none(report_id),
            _scalar(total),
            _all(rows),
        ]
        return mock_db

    def test_answers_actions_total_limit_offset(self, client):
        report_id = uuid4()
        mock_db = self._db(report_id, [_action(11), _action(12)], 7)
        _install(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report_id}/actions?limit=2&offset=4")

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"actions", "total", "limit", "offset"}
        assert (body["total"], body["limit"], body["offset"]) == (7, 2, 4)
        assert [a["id"] for a in body["actions"]] == [11, 12]
        assert set(body["actions"][0]) == {
            "id",
            "phase",
            "action_type",
            "memory_id",
            "target_id",
            "details",
            "created_at",
        }
        assert body["actions"][0]["created_at"].endswith("Z")

    def test_page_statement_is_scoped_ordered_and_bounded(self, client):
        report_id = uuid4()
        mock_db = self._db(report_id, [], 0)
        _install(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report_id}/actions?limit=3&offset=6")

        assert response.status_code == 200, response.text
        count_sql, page_sql = _sql(mock_db, 1), _sql(mock_db, 2)
        assert count_sql.lower().startswith("select count(")
        assert "sleep_actions.report_id =" in count_sql
        assert report_id in _params(mock_db, 1).values()
        assert "sleep_actions.report_id =" in page_sql
        assert "ORDER BY sleep_actions.id" in page_sql
        assert " DESC" not in page_sql
        assert report_id in _params(mock_db, 2).values()
        assert {3, 6} <= set(_params(mock_db, 2).values())
        # No row of another table, and no column the answer does not carry.
        assert "sleep_actions.report_id," not in page_sql.split("FROM")[0]

    def test_defaults_are_50_and_0(self, client):
        report_id = uuid4()
        mock_db = self._db(report_id, [], 0)
        _install(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report_id}/actions")

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["limit"], body["offset"]) == (50, 0)
        assert {50, 0} <= set(_params(mock_db, 2).values())

    @pytest.mark.parametrize("limit", [1, 500])
    def test_limit_bounds_are_accepted(self, client, limit):
        report_id = uuid4()
        mock_db = self._db(report_id, [], 0)
        _install(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report_id}/actions?limit={limit}")

        assert response.status_code == 200, response.text
        assert response.json()["limit"] == limit
        # The page statement is bound to the asked limit itself, not a smaller one.
        assert limit in set(_params(mock_db, 2).values())

    def test_the_largest_offset_is_bound_as_asked(self, client):
        report_id = uuid4()
        mock_db = self._db(report_id, [], 0)
        _install(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report_id}/actions?offset={2**63 - 1}")

        assert response.status_code == 200, response.text
        assert 2**63 - 1 in set(_params(mock_db, 2).values())

    @pytest.mark.parametrize(
        "query",
        [
            "limit=0",
            "limit=501",
            "limit=-1",
            "limit=abc",
            "offset=-1",
            "offset=abc",
            # past what the database takes as an OFFSET
            "offset=9223372036854775808",
            "offset=99999999999999999999",
        ],
    )
    def test_out_of_range_paging_is_422_not_clamped(self, client, query):
        mock_db = AsyncMock()
        _install(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{uuid4()}/actions?{query}")

        assert response.status_code == 422, response.text
        mock_db.execute.assert_not_awaited()

    def test_report_id_that_is_not_a_uuid_is_422(self, client):
        mock_db = AsyncMock()
        _install(mock_db)

        response = client.get("/api/v1/admin/sleep-reports/not-a-uuid/actions")

        assert response.status_code == 422
        mock_db.execute.assert_not_awaited()

    def test_unknown_report_is_404_with_the_detail_routes_body(self, client):
        mock_db = AsyncMock()
        mock_db.execute.side_effect = [_scalar_one_or_none(None)]
        _install(mock_db)
        report_id = uuid4()

        response = client.get(f"/api/v1/admin/sleep-reports/{report_id}/actions")

        assert response.status_code == 404
        assert response.json() == {
            "error": "HTTP-404",
            "message": f"Sleep report {report_id} not found.",
            "details": {},
        }
        # Nothing is read from sleep_actions for a report that does not exist.
        assert mock_db.execute.await_count == 1
        assert "sleep_actions" not in _sql(mock_db, 0)

    def test_a_read_writes_nothing(self, client):
        report_id = uuid4()
        mock_db = self._db(report_id, [_action(1)], 1)
        mock_db.add = MagicMock()
        _install(mock_db)

        response = client.get(f"/api/v1/admin/sleep-reports/{report_id}/actions")

        assert response.status_code == 200, response.text
        mock_db.add.assert_not_called()
        mock_db.commit.assert_not_awaited()
        mock_db.flush.assert_not_awaited()


class TestActionsPageGate:
    """The new route behind the real ``require_admin`` in each mode."""

    def _db(self):
        mock_db = AsyncMock()
        mock_db.execute.side_effect = [_scalar_one_or_none(uuid4()), _scalar(1), _all([_action(5)])]
        return mock_db

    def test_session_mode_answers_a_system_admin(self, client, gate):
        gate("session")
        _install(self._db(), admin=False)
        app.dependency_overrides[get_current_user_optional] = lambda: dict(ADMIN_SESSION)

        response = client.get(f"/api/v1/admin/sleep-reports/{uuid4()}/actions")

        assert response.status_code == 200, response.text
        assert [a["id"] for a in response.json()["actions"]] == [5]

    def test_service_mode_answers_an_operator(self, client, gate):
        token = secrets.token_hex(32)
        gate("service", token)
        _install(self._db(), admin=False)

        response = client.get(
            f"/api/v1/admin/sleep-reports/{uuid4()}/actions",
            headers={"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR},
        )

        assert response.status_code == 200, response.text
        assert [a["id"] for a in response.json()["actions"]] == [5]

    def test_service_mode_detail_without_actions_answers_an_operator(self, client, gate):
        token = secrets.token_hex(32)
        gate("service", token)
        report = _report()
        mock_db = AsyncMock()
        mock_db.execute.side_effect = [_scalar_one_or_none(report), _scalar(9), _all([])]
        _install(mock_db, admin=False)

        response = client.get(
            f"/api/v1/admin/sleep-reports/{report.id}?include_actions=false",
            headers={"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR},
        )

        assert response.status_code == 200, response.text
        assert response.json()["action_count"] == 9

    def test_off_mode_is_an_unknown_path(self, client, gate):
        gate("off")
        mock_db = AsyncMock()
        _install(mock_db, admin=False)
        app.dependency_overrides[get_current_user_optional] = lambda: dict(ADMIN_SESSION)

        response = client.get(f"/api/v1/admin/sleep-reports/{uuid4()}/actions")

        assert response.status_code == 404
        assert response.json() == {"error": "HTTP-404", "message": "Not Found", "details": {}}
        mock_db.execute.assert_not_awaited()
