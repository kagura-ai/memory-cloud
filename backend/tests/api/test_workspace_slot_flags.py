"""The additive workspace-slot fields of three existing responses (#2918).

- ``GET /api/v1/system/info``: ``features.workspace_slots``;
- ``GET /api/v1/auth/me``: ``user.workspace_selection_required``;
- ``GET /api/v1/workspaces``: ``slot_suspended`` on every row.

Each is false in ``off`` and ``shadow``, where no statement may reach the slot
tables; in ``enforce`` they read ``services.workspace_slots``. The same fields
on real rows, with a statement recorder, are in
``tests/integration/test_me_workspace_slots_db.py``.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.main import app
from config.settings import Settings, get_settings
from db.base import get_db

MODES = ["off", "shadow", "enforce"]


def _settings(mode: str) -> Settings:
    return Settings(
        _env_file=None, workspace_slot_suspension=mode, enforce_workspace_cap=(mode == "enforce")
    )


@pytest.mark.parametrize("mode", MODES)
def test_system_info_workspace_slots_is_true_only_when_enforced(monkeypatch, mode) -> None:
    settings = _settings(mode)
    monkeypatch.setattr("config.settings.get_settings", lambda: settings)
    body = TestClient(app).get("/api/v1/system/info").json()
    assert body["features"]["workspace_slots"] is (mode == "enforce")


def test_system_info_workspace_slots_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("WORKSPACE_SLOT_SUSPENSION", raising=False)
    settings = Settings(_env_file=None)
    monkeypatch.setattr("config.settings.get_settings", lambda: settings)
    assert TestClient(app).get("/api/v1/system/info").json()["features"]["workspace_slots"] is False


class _Workspace:
    def __init__(self) -> None:
        self.id = uuid4()
        self.name = "W"
        self.description = None
        self.owner_user_id = "someone"
        self.plan_name = "free"
        self.effective_max_contexts = 1
        self.created_at = datetime(2026, 1, 1)


@pytest.fixture
def list_client(monkeypatch):
    """GET /workspaces with the workspace service patched: two rows."""
    rows = [_Workspace(), _Workspace()]
    service = MagicMock()
    service.list_user_workspaces = AsyncMock(return_value=rows)
    service.get_workspace_stats = AsyncMock(return_value={"member_count": 2, "context_count": 0})
    service.get_member = AsyncMock(return_value=MagicMock(role="viewer"))
    monkeypatch.setattr("api.routes.workspaces.WorkspaceService", lambda db: service)
    monkeypatch.setattr(
        "api.routes.workspaces.get_current_user",
        AsyncMock(return_value={"user_id": "member-1", "email": "m@example.com"}),
    )
    session = MagicMock()
    session.execute = AsyncMock()

    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    yield rows, session
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_workspace_list_rows_are_not_suspended_and_no_statement_runs(
    list_client, monkeypatch, mode
) -> None:
    rows, session = list_client
    monkeypatch.setattr(get_settings(), "workspace_slot_suspension", mode)

    body = TestClient(app).get("/api/v1/workspaces").json()

    assert [row["slot_suspended"] for row in body] == [False, False]
    session.execute.assert_not_awaited()


def test_workspace_list_marks_suspended_rows_with_one_lookup(list_client, monkeypatch) -> None:
    rows, session = list_client
    monkeypatch.setattr(get_settings(), "enforce_workspace_cap", True)
    monkeypatch.setattr(get_settings(), "workspace_slot_suspension", "enforce")
    result = MagicMock()
    result.scalars.return_value.all.return_value = [rows[1].id]
    session.execute = AsyncMock(return_value=result)

    body = TestClient(app).get("/api/v1/workspaces").json()

    assert [row["slot_suspended"] for row in body] == [False, True]
    assert session.execute.await_count == 1
    statement = str(session.execute.await_args.args[0])
    assert "workspace_slot_suspensions" in statement


@pytest.mark.parametrize("mode", MODES)
def test_auth_me_carries_workspace_selection_required(monkeypatch, mode) -> None:
    """The flag sits inside ``user``; it is what ``owner_selection_required``
    answers (False with no statement unless enforced, pinned by
    ``tests/services/test_workspace_slots_read.py``)."""
    from auth.dependencies import require_session_auth

    monkeypatch.setattr(get_settings(), "enforce_workspace_cap", mode == "enforce")
    monkeypatch.setattr(get_settings(), "workspace_slot_suspension", mode)
    probe = AsyncMock(return_value=(mode == "enforce"))
    monkeypatch.setattr("api.routes.auth.owner_selection_required", probe)
    monkeypatch.setattr("api.routes.auth.linked_user_ids", AsyncMock(return_value=frozenset()))
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)

    async def _db():
        yield session

    async def _session() -> dict:
        return {"user_id": "owner-1", "email": "o@example.com", "current_workspace_id": None}

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_session_auth] = _session
    try:
        body = TestClient(app).get("/api/v1/auth/me").json()
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_session_auth, None)

    assert body["user"]["workspace_selection_required"] is (mode == "enforce")
    assert probe.await_args.args[1] == "owner-1"
