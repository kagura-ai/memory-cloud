"""Workspace slot account routes and flags on real rows (#2918).

- ``GET /api/v1/me/workspace-slots/workspaces/{id}/contexts``: the export rule
  (other creators' private contexts and deleted contexts left out) and one 404
  body for a workspace that is not owned, deleted or unknown.
- ``/auth/me`` ``workspace_selection_required``, ``GET /workspaces``
  ``slot_suspended``: false in ``off`` and ``shadow`` with NO statement on the
  slot tables (a statement recorder on the engine), read from the rows in
  ``enforce``.

Run on a private database only (``REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=...``).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from api.main import app
from auth.dependencies import require_session_auth
from config.settings import get_settings
from db.base import get_db
from models.auth import WorkspaceMember
from models.workspace_slots import WorkspaceSlotState, WorkspaceSlotSuspension
from utils.datetime import utcnow

from ._admin_helpers import make_context, make_user, make_workspace

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/me/workspace-slots"
SLOT_TABLES = ("workspace_slot_states", "workspace_slot_suspensions")


@pytest.fixture
def mode(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    def _set(value: str) -> None:
        monkeypatch.setattr(get_settings(), "enforce_workspace_cap", value == "enforce")
        monkeypatch.setattr(get_settings(), "workspace_slot_suspension", value)

    return _set


@asynccontextmanager
async def _http(db: AsyncSession, user_id: str) -> AsyncGenerator[httpx.AsyncClient, None]:
    principal = {"user_id": user_id, "email": f"{user_id}@example.com", "role": "user"}

    async def _db() -> AsyncGenerator[AsyncSession, None]:
        yield db

    async def _session() -> dict:
        return dict(principal, current_workspace_id=None)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[require_session_auth] = _session
    try:
        with patch(
            "api.routes.workspaces.get_current_user", AsyncMock(return_value=dict(principal))
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                yield client
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_session_auth, None)


async def _statements(engine: AsyncEngine, call: Callable[[], Awaitable[Any]]) -> list[str]:
    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:
        seen.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        await call()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)
    return seen


def _touches_slot_tables(statements: list[str]) -> list[str]:
    return [s for s in statements if any(table in s for table in SLOT_TABLES)]


async def _world(db: AsyncSession) -> dict[str, Any]:
    """An owner of two Free workspaces (one suspended, selection required), a
    member of the suspended one, and contexts of every kind in it."""
    owner = make_user(name="Slot Owner")
    member = make_user(name="Slot Member")
    stranger = make_user(name="Stranger")
    db.add_all([owner, member, stranger])
    await db.flush()
    active = make_workspace(owner_user_id=owner.user_id, plan_name="free")
    suspended = make_workspace(owner_user_id=owner.user_id, plan_name="free")
    gone = make_workspace(owner_user_id=owner.user_id, plan_name="free", soft_deleted=True)
    other = make_workspace(owner_user_id=stranger.user_id, plan_name="free")
    db.add_all([active, suspended, gone, other])
    await db.flush()
    db.add_all(
        [
            WorkspaceMember(workspace_id=active.id, user_id=owner.user_id, role="owner"),
            WorkspaceMember(workspace_id=suspended.id, user_id=owner.user_id, role="owner"),
            WorkspaceMember(workspace_id=suspended.id, user_id=member.user_id, role="member"),
            WorkspaceMember(workspace_id=other.id, user_id=owner.user_id, role="member"),
            WorkspaceMember(workspace_id=other.id, user_id=stranger.user_id, role="owner"),
        ]
    )
    shared = make_context(workspace_id=suspended.id, created_by=member.user_id)
    shared.name = "b-shared"
    shared.display_name = "Shared notes"
    own_private = make_context(workspace_id=suspended.id, created_by=owner.user_id, is_private=True)
    own_private.name = "a-own-private"
    member_private = make_context(
        workspace_id=suspended.id, created_by=member.user_id, is_private=True
    )
    deleted = make_context(workspace_id=suspended.id, created_by=owner.user_id)
    deleted.deleted_at = utcnow()
    db.add_all([shared, own_private, member_private, deleted])
    now = utcnow()
    db.add(
        WorkspaceSlotSuspension(
            workspace_id=suspended.id,
            owner_user_id=owner.user_id,
            cause="billing_snapshot",
            suspended_at=now,
        )
    )
    db.add(
        WorkspaceSlotState(
            owner_user_id=owner.user_id, selection_required_since=now, updated_at=now
        )
    )
    await db.commit()
    return {
        "owner": owner,
        "member": member,
        "active": active,
        "suspended": suspended,
        "gone": gone,
        "other": other,
        "shared": shared,
        "own_private": own_private,
    }


# ---------------------------------------------------------------------------
# Contexts route
# ---------------------------------------------------------------------------


async def test_contexts_of_a_suspended_owned_workspace_follow_the_export_rule(
    db_session: AsyncSession, mode
) -> None:
    mode("enforce")
    world = await _world(db_session)

    async with _http(db_session, world["owner"].user_id) as client:
        response = await client.get(f"{BASE}/workspaces/{world['suspended'].id}/contexts")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "workspace_id": str(world["suspended"].id),
        "contexts": [
            {
                "id": str(world["own_private"].id),
                "name": "a-own-private",
                "display_name": None,
                "is_private": True,
            },
            {
                "id": str(world["shared"].id),
                "name": "b-shared",
                "display_name": "Shared notes",
                "is_private": False,
            },
        ],
    }


async def test_contexts_of_an_active_owned_workspace_without_contexts_is_an_empty_list(
    db_session: AsyncSession, mode
) -> None:
    mode("enforce")
    world = await _world(db_session)

    async with _http(db_session, world["owner"].user_id) as client:
        response = await client.get(f"{BASE}/workspaces/{world['active'].id}/contexts")

    assert response.status_code == 200
    assert response.json() == {"workspace_id": str(world["active"].id), "contexts": []}


async def test_contexts_answer_one_404_for_not_owned_deleted_and_unknown(
    db_session: AsyncSession, mode
) -> None:
    mode("enforce")
    world = await _world(db_session)

    async with _http(db_session, world["owner"].user_id) as client:
        member_of = await client.get(f"{BASE}/workspaces/{world['other'].id}/contexts")
        deleted = await client.get(f"{BASE}/workspaces/{world['gone'].id}/contexts")
        unknown = await client.get(f"{BASE}/workspaces/{uuid4()}/contexts")
    async with _http(db_session, world["member"].user_id) as client:
        as_member = await client.get(f"{BASE}/workspaces/{world['suspended'].id}/contexts")

    expected = {"error": "RES-001", "message": "Workspace not found", "details": {}}
    for response in (member_of, deleted, unknown, as_member):
        assert response.status_code == 404
        assert response.json() == expected


@pytest.mark.parametrize("value", ["off", "shadow"])
async def test_contexts_route_runs_no_statement_unless_enforced(
    db_session: AsyncSession, async_engine: AsyncEngine, mode, value: str
) -> None:
    world = await _world(db_session)
    mode(value)

    async with _http(db_session, world["owner"].user_id) as client:
        holder: dict[str, httpx.Response] = {}

        async def _call() -> None:
            holder["r"] = await client.get(f"{BASE}/workspaces/{world['suspended'].id}/contexts")

        statements = await _statements(async_engine, _call)

    assert holder["r"].status_code == 404
    assert holder["r"].json()["error"] == "HTTP-404"
    assert statements == []


# ---------------------------------------------------------------------------
# /auth/me and GET /workspaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["off", "shadow"])
async def test_flags_are_false_and_the_slot_tables_are_not_read(
    db_session: AsyncSession, async_engine: AsyncEngine, mode, value: str
) -> None:
    world = await _world(db_session)
    mode(value)
    bodies: dict[str, Any] = {}

    async with _http(db_session, world["owner"].user_id) as client:

        async def _call() -> None:
            bodies["me"] = (await client.get("/api/v1/auth/me")).json()
            bodies["list"] = (await client.get("/api/v1/workspaces")).json()

        statements = await _statements(async_engine, _call)

    assert statements, "the recorder saw the requests"
    assert _touches_slot_tables(statements) == []
    assert bodies["me"]["user"]["workspace_selection_required"] is False
    assert {row["id"]: row["slot_suspended"] for row in bodies["list"]} == {
        str(world["active"].id): False,
        str(world["suspended"].id): False,
        str(world["other"].id): False,
    }


async def test_flags_read_the_rows_when_enforced(
    db_session: AsyncSession, async_engine: AsyncEngine, mode
) -> None:
    world = await _world(db_session)
    mode("enforce")
    bodies: dict[str, Any] = {}

    async with _http(db_session, world["owner"].user_id) as client:

        async def _call() -> None:
            bodies["me"] = (await client.get("/api/v1/auth/me")).json()
            bodies["list"] = (await client.get("/api/v1/workspaces")).json()

        statements = await _statements(async_engine, _call)
    async with _http(db_session, world["member"].user_id) as client:
        member_me = (await client.get("/api/v1/auth/me")).json()
        member_list = (await client.get("/api/v1/workspaces")).json()

    assert bodies["me"]["user"]["workspace_selection_required"] is True
    assert {row["id"]: row["slot_suspended"] for row in bodies["list"]} == {
        str(world["active"].id): False,
        str(world["suspended"].id): True,
        str(world["other"].id): False,
    }
    # One lookup for the whole list (plus the one of /auth/me).
    assert len(_touches_slot_tables(statements)) == 2
    # A member sees the suspended workspace marked too; the selection is not theirs.
    assert member_me["user"]["workspace_selection_required"] is False
    assert [row["slot_suspended"] for row in member_list] == [True]


async def test_selection_required_is_false_once_nothing_is_suspended(
    db_session: AsyncSession, mode
) -> None:
    world = await _world(db_session)
    await db_session.delete(await db_session.get(WorkspaceSlotSuspension, world["suspended"].id))
    await db_session.commit()
    mode("enforce")

    async with _http(db_session, world["owner"].user_id) as client:
        me = (await client.get("/api/v1/auth/me")).json()

    assert me["user"]["workspace_selection_required"] is False
