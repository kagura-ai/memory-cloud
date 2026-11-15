"""Run Sleep and undo a merge for a target user (#2838).

- ``POST /api/v1/admin/users/{user_id}/sleep/run``: one run for the target
  user's context. The report and the job carry the target, never the caller;
  an ``audit_logs`` row (``sleep_run_trigger``) is added before the handler's
  only commit and names the caller as actor and the target in its metadata.
- ``POST /api/v1/admin/sleep-reports/{report_id}/actions/{action_id}/undo-merge``:
  the undo service is called with the report id and the actor; every error
  code keeps its status.

Both take ``require_admin``: a session admin and a service operator are
served, and the two own-account routes keep refusing an operator. The
database is mocked here; eligibility, the restore and the audit rows on real
Postgres are in ``tests/integration/test_admin_sleep_target_db.py``.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from api.main import app
from api.routes import admin_sleep
from auth.dependencies import get_current_user_optional, require_admin
from auth.system_admin_mode import build_operator_principal
from config.settings import get_settings
from db.base import get_db
from models.auth import AuditLog
from services.sleep.undo import UndoMergeError

TARGET = "target-user-1"
ADMIN_SESSION = {"user_id": "admin-1001", "email": "admin@example.com", "role": "admin"}
OPERATOR_ADDRESS = "ops@example.com"
OPERATOR = build_operator_principal(OPERATOR_ADDRESS)
PRINCIPALS = {"session": ADMIN_SESSION, "service": OPERATOR}
RUN_URL = f"/api/v1/admin/users/{TARGET}/sleep/run"


def _undo_url(report_id: UUID, action_id: int = 42) -> str:
    return f"/api/v1/admin/sleep-reports/{report_id}/actions/{action_id}/undo-merge"


def _scalar_one_or_none(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _first(value):
    result = MagicMock()
    result.first.return_value = value
    return result


def _sql(mock_db, index: int) -> str:
    stmt = mock_db.execute.await_args_list[index].args[0]
    return " ".join(str(stmt.compile(dialect=postgresql.dialect())).split())


def _bound(mock_db, index: int) -> list:
    stmt = mock_db.execute.await_args_list[index].args[0]
    return list(stmt.compile(dialect=postgresql.dialect()).params.values())


@pytest.fixture
def client():
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


def _install(mock_db, principal: dict | None) -> None:
    async def _get_db():
        yield mock_db

    app.dependency_overrides[get_db] = _get_db
    if principal is not None:
        app.dependency_overrides[require_admin] = lambda: dict(principal)


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


@pytest.fixture
def events() -> list:
    """What the handler did to the session and the scheduler, in order."""
    return []


@pytest.fixture
def scheduled(monkeypatch, events) -> MagicMock:
    """Record the batch a handler schedules without running it."""
    batch = MagicMock(name="_run_sleep_batch")

    def fake_create_task(job, *_args, **_kwargs):
        events.append("task")
        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = None
        return task

    monkeypatch.setattr(admin_sleep, "_run_sleep_batch", batch)
    monkeypatch.setattr(admin_sleep.asyncio, "create_task", fake_create_task)
    return batch


@pytest.fixture
def created(monkeypatch, events) -> list[dict]:
    """Replace ``SleepReporter.create_report``: the mocked session assigns no id."""
    made: list[dict] = []

    async def fake_create_report(_self, user_id, workspace_id=None, context_id=None):
        report = MagicMock()
        report.id = uuid4()
        made.append(
            {
                "id": report.id,
                "user_id": user_id,
                "workspace_id": workspace_id,
                "context_id": context_id,
            }
        )
        events.append("report")
        return report

    monkeypatch.setattr(admin_sleep.SleepReporter, "create_report", fake_create_report)
    return made


def _run_db(events, *, user=TARGET, running=None, eligible=True) -> tuple[AsyncMock, UUID, UUID]:
    """Session mock for the run handler: user, running report, eligibility."""
    workspace_id, context_id = uuid4(), uuid4()
    mock_db = AsyncMock()
    effects = [_scalar_one_or_none(user)]
    if user is not None:
        effects.append(_first(running))
        if running is None:
            effects.append(_first((workspace_id, context_id) if eligible else None))
    mock_db.execute.side_effect = effects
    mock_db.add = MagicMock(side_effect=lambda obj: events.append(("add", obj)))
    mock_db.commit.side_effect = lambda: events.append("commit")
    return mock_db, workspace_id, context_id


class TestRunForTargetUser:
    """POST /api/v1/admin/users/{user_id}/sleep/run"""

    @pytest.mark.parametrize("mode", sorted(PRINCIPALS))
    def test_runs_one_job_for_the_target_and_audits_the_caller(
        self, client, events, scheduled, created, mode
    ):
        principal = PRINCIPALS[mode]
        mock_db, workspace_id, context_id = _run_db(events)
        _install(mock_db, principal)

        response = client.post(RUN_URL, json={"context_id": str(context_id)})

        assert response.status_code == 202, response.text
        [report] = created
        assert response.json() == {"report_ids": [str(report["id"])]}
        # The report and the job carry the target user, not the caller.
        assert report["user_id"] == TARGET
        assert report["workspace_id"] == str(workspace_id)
        assert report["context_id"] == str(context_id)
        scheduled.assert_called_once_with(
            [(report["id"], str(workspace_id), str(context_id))], TARGET
        )
        assert principal["user_id"] not in repr(scheduled.call_args)
        # One audit row: the caller as actor, the target in the metadata.
        [audit] = [e[1] for e in events if isinstance(e, tuple)]
        assert isinstance(audit, AuditLog)
        assert audit.action == "sleep_run_trigger"
        assert audit.user_id == principal["user_id"]
        assert audit.user_email == principal["email"]
        assert audit.resource == f"context:{context_id}"
        assert audit.user_metadata == {
            "target_user_id": TARGET,
            "workspace_id": str(workspace_id),
            "context_id": str(context_id),
            "report_id": str(report["id"]),
        }
        assert audit.ip_address is None and audit.user_agent is None
        # The report, then the audit row, then the only commit, then the task.
        assert [e if isinstance(e, str) else e[0] for e in events] == [
            "report",
            "add",
            "commit",
            "task",
        ]
        assert mock_db.commit.await_count == 1

    def test_the_operators_audit_row_carries_the_operator_id(
        self, client, events, scheduled, created
    ):
        mock_db, _, context_id = _run_db(events)
        _install(mock_db, OPERATOR)

        response = client.post(RUN_URL, json={"context_id": str(context_id)})

        assert response.status_code == 202, response.text
        [audit] = [e[1] for e in events if isinstance(e, tuple)]
        assert audit.user_id == f"operator:{OPERATOR_ADDRESS}"
        assert audit.user_email == f"operator:{OPERATOR_ADDRESS}"
        assert audit.user_metadata["target_user_id"] == TARGET
        # The actor is in the actor columns only.
        assert OPERATOR_ADDRESS not in json.dumps(audit.user_metadata)
        assert OPERATOR_ADDRESS not in response.text

    def test_the_checks_run_in_order_on_the_target(self, client, events, scheduled, created):
        mock_db, _, context_id = _run_db(events)
        _install(mock_db, ADMIN_SESSION)

        response = client.post(RUN_URL, json={"context_id": str(context_id)})

        assert response.status_code == 202, response.text
        assert mock_db.execute.await_count == 3
        user_sql, running_sql, eligible_sql = (_sql(mock_db, i) for i in range(3))
        # (a) the target's users row, by its id.
        assert "FROM users" in user_sql and "users.user_id =" in user_sql
        assert _bound(mock_db, 0) == [TARGET]
        # (b) a running report of the target, never of the caller.
        assert "sleep_reports.user_id =" in running_sql
        assert "sleep_reports.status =" in running_sql
        assert TARGET in _bound(mock_db, 1)
        assert ADMIN_SESSION["user_id"] not in _bound(mock_db, 1)
        # (c) one statement: the live, non-skip context with a live memory of
        # the target in the context's own workspace.
        assert "contexts.id =" in eligible_sql
        assert "contexts.deleted_at IS NULL" in eligible_sql
        assert "contexts.sleep_mode !=" in eligible_sql
        assert "EXISTS (SELECT" in eligible_sql
        assert "memories.context_id = contexts.id" in eligible_sql
        assert "memories.workspace_id = contexts.workspace_id" in eligible_sql
        assert "memories.user_id =" in eligible_sql
        assert "memories.deleted_at IS NULL" in eligible_sql
        assert "contexts.created_by" not in eligible_sql
        assert {TARGET, context_id, "skip"} <= set(_bound(mock_db, 2))
        assert ADMIN_SESSION["user_id"] not in _bound(mock_db, 2)

    def test_a_running_report_of_the_target_is_409(self, client, events, scheduled, created):
        running_id, started_at = uuid4(), datetime(2026, 4, 9, 11, 30, 0)
        mock_db, _, _ = _run_db(events, running=(running_id, started_at))
        _install(mock_db, OPERATOR)

        response = client.post(RUN_URL, json={"context_id": str(uuid4())})

        assert response.status_code == 409, response.text
        body = response.json()
        assert body["error"] == "SLEEP-002"
        assert body["message"] == "A sleep run is already in progress for this user."
        assert body["details"] == {
            "running_report_id": str(running_id),
            "started_at": "2026-04-09T11:30:00Z",
        }
        assert events == [] and created == []
        scheduled.assert_not_called()
        mock_db.commit.assert_not_awaited()

    def test_a_running_report_of_the_caller_alone_is_no_conflict(
        self, client, events, scheduled, created
    ):
        """The guard is keyed on the target: the lookup binds the target's id,
        so the caller's own running report cannot answer it."""
        mock_db, _, context_id = _run_db(events)
        _install(mock_db, ADMIN_SESSION)

        response = client.post(RUN_URL, json={"context_id": str(context_id)})

        assert response.status_code == 202, response.text
        assert _bound(mock_db, 1).count(TARGET) == 1
        assert ADMIN_SESSION["user_id"] not in _bound(mock_db, 1)

    def test_an_unknown_user_is_404_without_the_sleep_code(
        self, client, events, scheduled, created
    ):
        mock_db, _, _ = _run_db(events, user=None)
        _install(mock_db, OPERATOR)

        response = client.post(RUN_URL, json={"context_id": str(uuid4())})

        assert response.status_code == 404, response.text
        assert response.json() == {"error": "RES-001", "message": "User not found", "details": {}}
        assert "SLEEP-003" not in response.text
        # Nothing after the users lookup, and nothing written.
        assert mock_db.execute.await_count == 1
        assert events == [] and created == []
        scheduled.assert_not_called()
        mock_db.commit.assert_not_awaited()

    def test_a_context_that_is_not_eligible_is_404_sleep_003(
        self, client, events, scheduled, created
    ):
        mock_db, _, _ = _run_db(events, eligible=False)
        _install(mock_db, OPERATOR)
        context_id = uuid4()

        response = client.post(RUN_URL, json={"context_id": str(context_id)})

        assert response.status_code == 404, response.text
        body = response.json()
        assert body["error"] == "SLEEP-003"
        assert body["details"] == {"context_id": str(context_id)}
        assert TARGET not in body["message"]
        assert events == [] and created == []
        scheduled.assert_not_called()
        mock_db.commit.assert_not_awaited()

    @pytest.mark.parametrize(
        "body", [{}, {"context_id": None}, {"context_id": "not-a-uuid"}, {"context_id": ""}]
    )
    def test_context_id_is_required(self, client, events, scheduled, created, body):
        mock_db, _, _ = _run_db(events)
        _install(mock_db, OPERATOR)

        response = client.post(RUN_URL, json=body)

        assert response.status_code == 422, response.text
        mock_db.execute.assert_not_awaited()
        assert events == [] and created == []
        scheduled.assert_not_called()

    def test_no_body_is_422(self, client, events, scheduled, created):
        mock_db, _, _ = _run_db(events)
        _install(mock_db, OPERATOR)

        response = client.post(RUN_URL)

        assert response.status_code == 422, response.text
        mock_db.execute.assert_not_awaited()

    def test_the_log_line_names_ids_only_and_never_the_operators_address(
        self, client, events, scheduled, created
    ):
        from structlog.testing import capture_logs

        mock_db, _, context_id = _run_db(events)
        _install(mock_db, OPERATOR)

        with capture_logs() as logs:
            response = client.post(RUN_URL, json={"context_id": str(context_id)})

        assert response.status_code == 202, response.text
        [entry] = [e for e in logs if e["event"] == "target_sleep_run_scheduled"]
        assert entry == {
            "event": "target_sleep_run_scheduled",
            "log_level": "info",
            "target_user_id": TARGET,
            "context_id": str(context_id),
            "report_id": str(created[0]["id"]),
            "operator": True,
        }
        assert OPERATOR_ADDRESS not in repr(logs)

    def test_the_handler_never_reads_the_callers_users_row(
        self, client, events, scheduled, created
    ):
        mock_db, _, context_id = _run_db(events)
        _install(mock_db, OPERATOR)

        response = client.post(RUN_URL, json={"context_id": str(context_id)})

        assert response.status_code == 202, response.text
        for index in range(mock_db.execute.await_count):
            assert OPERATOR["user_id"] not in _bound(mock_db, index)
            assert OPERATOR_ADDRESS not in _bound(mock_db, index)


class TestUndoForTargetReport:
    """POST /api/v1/admin/sleep-reports/{report_id}/actions/{action_id}/undo-merge"""

    @pytest.fixture
    def undo(self, monkeypatch) -> AsyncMock:
        summary = {
            "restored_memory_id": str(uuid4()),
            "winner_id": str(uuid4()),
            "report_id": str(uuid4()),
            "undone_action_id": 42,
        }
        stub = AsyncMock(return_value=summary)
        monkeypatch.setattr("services.sleep.undo.undo_merge_action", stub)
        return stub

    @pytest.mark.parametrize("mode", sorted(PRINCIPALS))
    def test_the_undo_is_asked_for_the_report_with_the_actor(self, client, undo, mode):
        principal = PRINCIPALS[mode]
        mock_db = AsyncMock()
        _install(mock_db, principal)
        report_id = uuid4()

        response = client.post(_undo_url(report_id, 42))

        assert response.status_code == 200, response.text
        assert response.json() == undo.return_value
        assert set(response.json()) == {
            "restored_memory_id",
            "winner_id",
            "report_id",
            "undone_action_id",
        }
        undo.assert_awaited_once_with(
            mock_db,
            42,
            acting_user_id=principal["user_id"],
            target_report_id=report_id,
            actor_email=principal["email"],
        )

    @pytest.mark.parametrize(
        ("code", "status"),
        [
            ("action_not_found", 404),
            ("not_a_merge", 400),
            ("already_restored", 409),
            ("edge_changed", 409),
            ("not_merge_deleted", 409),
            ("memory_purged", 410),
            ("not_restorable", 400),
        ],
    )
    def test_each_error_code_keeps_its_status(self, client, undo, code, status):
        undo.side_effect = UndoMergeError(code, f"refused: {code}")
        _install(AsyncMock(), OPERATOR)

        response = client.post(_undo_url(uuid4()))

        assert response.status_code == status, response.text
        assert response.json() == {
            "error": f"SLEEP-UNDO-{code}",
            "message": f"refused: {code}",
            "details": {},
        }

    def test_the_statuses_are_the_own_account_routes(self):
        """Both undo routes map codes through one table."""
        assert admin_sleep._UNDO_ERROR_STATUS == {
            "action_not_found": 404,
            "memory_purged": 410,
            "already_restored": 409,
            "edge_changed": 409,
            "not_a_merge": 400,
            "not_merge_deleted": 409,
        }

    @pytest.mark.parametrize("path", ["not-a-uuid/actions/1", f"{uuid4()}/actions/abc"])
    def test_a_malformed_id_is_422(self, client, undo, path):
        _install(AsyncMock(), OPERATOR)

        response = client.post(f"/api/v1/admin/sleep-reports/{path}/undo-merge")

        assert response.status_code == 422, response.text
        undo.assert_not_awaited()

    @pytest.mark.parametrize("action_id", [0, -1, 2**63])
    def test_an_action_id_outside_the_column_is_422(self, client, undo, action_id):
        _install(AsyncMock(), OPERATOR)

        response = client.post(_undo_url(uuid4(), action_id))

        assert response.status_code == 422, response.text
        undo.assert_not_awaited()


class TestGate:
    """Both routes behind the real ``require_admin``; the old ones unchanged."""

    def test_service_mode_serves_an_operator_on_the_run_route(
        self, client, gate, events, scheduled, created
    ):
        token = secrets.token_hex(32)
        gate("service", token)
        mock_db, _, context_id = _run_db(events)
        _install(mock_db, None)

        response = client.post(
            RUN_URL,
            json={"context_id": str(context_id)},
            headers={"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR_ADDRESS},
        )

        assert response.status_code == 202, response.text
        [audit] = [e[1] for e in events if isinstance(e, tuple)]
        assert audit.user_id == f"operator:{OPERATOR_ADDRESS}"
        assert created[0]["user_id"] == TARGET

    def test_service_mode_serves_an_operator_on_the_undo_route(self, client, gate, monkeypatch):
        token = secrets.token_hex(32)
        gate("service", token)
        stub = AsyncMock(
            return_value={
                "restored_memory_id": str(uuid4()),
                "winner_id": None,
                "report_id": str(uuid4()),
                "undone_action_id": 7,
            }
        )
        monkeypatch.setattr("services.sleep.undo.undo_merge_action", stub)
        _install(AsyncMock(), None)

        response = client.post(
            _undo_url(uuid4(), 7),
            headers={"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR_ADDRESS},
        )

        assert response.status_code == 200, response.text
        assert stub.await_args.kwargs["acting_user_id"] == f"operator:{OPERATOR_ADDRESS}"
        assert stub.await_args.kwargs["actor_email"] == f"operator:{OPERATOR_ADDRESS}"

    def test_session_mode_serves_a_system_admin(self, client, gate, events, scheduled, created):
        gate("session")
        mock_db, _, context_id = _run_db(events)
        _install(mock_db, None)
        app.dependency_overrides[get_current_user_optional] = lambda: dict(ADMIN_SESSION)

        response = client.post(RUN_URL, json={"context_id": str(context_id)})

        assert response.status_code == 202, response.text
        assert created[0]["user_id"] == TARGET

    @pytest.mark.parametrize("url", [RUN_URL, _undo_url(uuid4())])
    def test_off_mode_is_an_unknown_path(self, client, gate, url):
        gate("off")
        mock_db = AsyncMock()
        _install(mock_db, None)
        app.dependency_overrides[get_current_user_optional] = lambda: dict(ADMIN_SESSION)

        response = client.post(url, json={"context_id": str(uuid4())})

        assert response.status_code == 404
        assert response.json() == {"error": "HTTP-404", "message": "Not Found", "details": {}}
        mock_db.execute.assert_not_awaited()

    def test_both_routes_are_on_the_admin_surface_and_not_own_account(self):
        from tests.api.test_system_admin_surface import ADMIN_SURFACE, OWN_ACCOUNT_ROUTES

        new = {
            ("POST", "/api/v1/admin/users/{user_id}/sleep/run"),
            ("POST", "/api/v1/admin/sleep-reports/{report_id}/actions/{action_id}/undo-merge"),
        }
        assert new <= ADMIN_SURFACE
        assert not new & OWN_ACCOUNT_ROUTES
        # The two own-account routes stay where they were.
        assert {
            ("POST", "/api/v1/admin/sleep/run"),
            ("POST", "/api/v1/admin/sleep/actions/{action_id}/undo-merge"),
        } <= OWN_ACCOUNT_ROUTES

    def test_the_own_account_routes_still_refuse_an_operator(self, client, gate):
        token = secrets.token_hex(32)
        gate("service", token)
        mock_db = AsyncMock()
        _install(mock_db, None)
        headers = {"Authorization": f"Bearer {token}", "X-Operator-Email": OPERATOR_ADDRESS}

        run = client.post("/api/v1/admin/sleep/run", json={}, headers=headers)
        undo = client.post("/api/v1/admin/sleep/actions/1/undo-merge", headers=headers)

        assert (run.status_code, undo.status_code) == (403, 403)
        assert run.json()["error"] == undo.json()["error"] == "SYSADMIN-004"
        mock_db.execute.assert_not_awaited()

    def test_the_own_account_undo_passes_no_report_and_no_actor_email(self, client, monkeypatch):
        """The own-account route keeps the caller filter and writes no audit row:
        it calls the service without the two target parameters."""
        stub = AsyncMock(
            return_value={
                "restored_memory_id": str(uuid4()),
                "winner_id": None,
                "report_id": str(uuid4()),
                "undone_action_id": 9,
            }
        )
        monkeypatch.setattr("services.sleep.undo.undo_merge_action", stub)
        mock_db = AsyncMock()
        _install(mock_db, ADMIN_SESSION)

        response = client.post("/api/v1/admin/sleep/actions/9/undo-merge")

        assert response.status_code == 200, response.text
        stub.assert_awaited_once_with(mock_db, 9, acting_user_id=ADMIN_SESSION["user_id"])
