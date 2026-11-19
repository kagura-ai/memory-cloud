"""The workspace slot gate module (#2918), without a database.

* Unless the mode is ``enforce`` the gate runs no statement and the SQL twin
  is ``None`` (the code is deployable before the migration; ``shadow`` ignores
  the rows).
* In ``enforce`` an active workspace costs one statement; a suspended one is
  refused with the details for the caller: the owner gets the account page,
  a member is told to ask the owner, anyone else gets the gate only.
* ``OWNER_MANAGEMENT`` lets the owner through and nobody else; a stray policy
  value fails closed.
* The MCP face: code ``workspace_suspended`` on every door, the dispatcher's
  target-less check opens no session unless enforced and fails closed.

The rule itself (stamped owner, live workspace, no billing contract) is pinned
on real rows in ``tests/integration/test_workspace_slots_db.py`` and the
surfaces in ``tests/integration/test_workspace_slot_gate_db.py``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from models.auth import Workspace
from services import workspace_slot_gate as gate
from services.workspace_slot_gate import SlotPolicy
from utils.exceptions import WorkspaceSlotSuspendedError

OWNER = "owner-1"


def _mode(mode: str):
    return patch("services.workspace_slots.workspace_slot_mode", return_value=mode)


def _db() -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock()
    db.scalar = AsyncMock()
    db.get = AsyncMock()
    return db


def _workspace() -> Workspace:
    return Workspace(id=uuid4(), name="w", owner_user_id=OWNER, plan_name="free")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_no_statement_and_no_predicate_unless_enforced(mode: str) -> None:
    db = _db()
    ws = _workspace()
    with (
        _mode(mode),
        patch("services.workspace_slot_gate.suspended_workspace_ids") as lookup,
    ):
        assert await gate.slot_suspended(db, ws.id) is False
        await gate.ensure_slot_active(db, ws, user_id=OWNER)
        await gate.ensure_slot_active(db, ws.id, user_id="someone", outsider=True)
        assert gate.active_workspace_predicate() is None
        assert gate.active_workspace_predicate(Workspace.id) is None
    lookup.assert_not_called()
    db.execute.assert_not_awaited()
    db.scalar.assert_not_awaited()
    db.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_active_workspace_costs_one_lookup() -> None:
    db = _db()
    ws = _workspace()
    lookup = AsyncMock(return_value=frozenset())
    with _mode("enforce"), patch("services.workspace_slot_gate.suspended_workspace_ids", lookup):
        await gate.ensure_slot_active(db, ws, user_id=OWNER)
    lookup.assert_awaited_once_with(db, [ws.id])
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_none_or_a_malformed_id_passes_without_a_statement() -> None:
    db = _db()
    lookup = AsyncMock(return_value=frozenset())
    with _mode("enforce"), patch("services.workspace_slot_gate.suspended_workspace_ids", lookup):
        await gate.ensure_slot_active(db, None, user_id=OWNER)
        await gate.ensure_slot_active(db, "not-a-uuid", user_id=OWNER)
        assert await gate.slot_suspended(db, None) is False
    lookup.assert_not_awaited()


def _suspended(ws_id):
    return patch(
        "services.workspace_slot_gate.suspended_workspace_ids",
        AsyncMock(return_value=frozenset({ws_id})),
    )


@pytest.mark.asyncio
async def test_the_owner_is_told_where_to_resume_it() -> None:
    db = _db()
    ws = _workspace()
    with _mode("enforce"), _suspended(ws.id), pytest.raises(WorkspaceSlotSuspendedError) as exc:
        await gate.ensure_slot_active(db, ws, user_id=OWNER)
    assert exc.value.error_code == "SLOT-001"
    assert exc.value.details["workspace_id"] == str(ws.id)
    assert exc.value.details["manage_url"] == gate.manage_url()
    assert gate.manage_url().endswith("/profile/workspaces")


@pytest.mark.asyncio
async def test_the_owner_of_an_id_is_read_from_the_row() -> None:
    db = _db()
    ws = _workspace()
    db.scalar = AsyncMock(return_value=OWNER)
    with _mode("enforce"), _suspended(ws.id), pytest.raises(WorkspaceSlotSuspendedError) as exc:
        await gate.ensure_slot_active(db, ws.id, user_id=OWNER)
    assert exc.value.details["manage_url"] == gate.manage_url()


@pytest.mark.asyncio
async def test_a_member_is_told_to_ask_the_owner() -> None:
    db = _db()
    ws = _workspace()
    with (
        _mode("enforce"),
        _suspended(ws.id),
        patch("services.workspace_slot_gate.is_workspace_member", AsyncMock(return_value=True)),
        pytest.raises(WorkspaceSlotSuspendedError) as exc,
    ):
        await gate.ensure_slot_active(db, ws, user_id="member-1")
    assert exc.value.details == {
        "gate": "workspace_slot",
        "workspace_id": str(ws.id),
        "manage_url": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "outsider", "member"),
    [("stranger", False, False), (None, False, False), (OWNER, True, True)],
    ids=["non-member", "no-user", "outsider-flag"],
)
async def test_anyone_else_gets_the_gate_only(user_id, outsider, member) -> None:
    db = _db()
    ws = _workspace()
    with (
        _mode("enforce"),
        _suspended(ws.id),
        patch("services.workspace_slot_gate.is_workspace_member", AsyncMock(return_value=member)),
        pytest.raises(WorkspaceSlotSuspendedError) as exc,
    ):
        await gate.ensure_slot_active(db, ws, user_id=user_id, outsider=outsider)
    assert exc.value.details == {"gate": "workspace_slot"}


@pytest.mark.asyncio
async def test_owner_management_lets_only_the_owner_through() -> None:
    db = _db()
    ws = _workspace()
    with (
        _mode("enforce"),
        _suspended(ws.id),
        patch("services.workspace_slot_gate.is_workspace_member", AsyncMock(return_value=True)),
    ):
        await gate.ensure_slot_active(db, ws, user_id=OWNER, policy=SlotPolicy.OWNER_MANAGEMENT)
        with pytest.raises(WorkspaceSlotSuspendedError):
            await gate.ensure_slot_active(
                db, ws, user_id="admin-1", policy=SlotPolicy.OWNER_MANAGEMENT
            )
        with pytest.raises(WorkspaceSlotSuspendedError):
            await gate.ensure_slot_active(
                db, ws, user_id=OWNER, policy=SlotPolicy.OWNER_MANAGEMENT, outsider=True
            )
        # Only the enum member opts out: a stray value fails closed.
        with pytest.raises(WorkspaceSlotSuspendedError):
            await gate.ensure_slot_active(db, ws, user_id=OWNER, policy="owner_management")  # type: ignore[arg-type]


def test_the_sql_twin_is_a_negation_when_enforced() -> None:
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    from models.memory import Memory

    with _mode("enforce"):
        own = gate.active_workspace_predicate()
        joined = gate.active_workspace_predicate(Memory.workspace_id)
    assert own is not None and joined is not None
    sql = str(select(Memory.id).where(joined).compile(dialect=postgresql.dialect()))
    assert "workspace_slot_suspensions" in sql
    assert sql.count("NOT (EXISTS") == 1


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------


def test_mcp_code_and_details_follow_the_audience() -> None:
    from mcp_server.tools._errors import describe_tool_exception

    wid = uuid4()
    owner = WorkspaceSlotSuspendedError.for_owner(wid, "https://app.example.com/profile/workspaces")
    member = WorkspaceSlotSuspendedError.for_member(wid)
    outsider = WorkspaceSlotSuspendedError.for_outsider()
    # A handler's legacy code never hides the suspension.
    body = json.loads(
        describe_tool_exception("recall", owner, error="recall_error").response()[0].text
    )
    assert body["error"] == "workspace_suspended"
    assert body["workspace_id"] == str(wid)
    assert body["manage_url"] == "https://app.example.com/profile/workspaces"
    assert body["help"].split(" at ", 1)[1].split(". ", 1)[0] == body["manage_url"]
    body = json.loads(describe_tool_exception("recall", member).response()[0].text)
    assert body["error"] == "workspace_suspended"
    assert "manage_url" not in body
    assert "owner" in body["help"]
    body = json.loads(describe_tool_exception("recall", outsider).response()[0].text)
    assert body["error"] == "workspace_suspended"
    assert "workspace_id" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_the_dispatcher_check_opens_no_session_unless_enforced(mode: str) -> None:
    from mcp_server.tools._slot_gate import slot_suspension_refusal

    with _mode(mode), patch("db.base.get_db") as get_db:
        assert await slot_suspension_refusal("recall", {}, OWNER, uuid4()) is None
    get_db.assert_not_called()


@pytest.mark.asyncio
async def test_the_dispatcher_leaves_named_targets_and_the_manuals_alone() -> None:
    from mcp_server.tools._slot_gate import slot_suspension_refusal

    with _mode("enforce"), patch("db.base.get_db") as get_db:
        assert (
            await slot_suspension_refusal("recall", {"context_id": str(uuid4())}, OWNER, uuid4())
            is None
        )
        assert await slot_suspension_refusal("guide", {}, OWNER, uuid4()) is None
        assert await slot_suspension_refusal("describe_tools", {}, OWNER, uuid4()) is None
        assert await slot_suspension_refusal("recall", {}, OWNER, None) is None
    get_db.assert_not_called()


@pytest.mark.asyncio
async def test_the_dispatcher_fails_closed() -> None:
    from mcp_server.tools._slot_gate import slot_suspension_refusal

    async def _broken():
        raise ConnectionError("database down")
        yield  # pragma: no cover

    with _mode("enforce"), patch("db.base.get_db", _broken):
        refusal = await slot_suspension_refusal("recall", {}, OWNER, uuid4())
    assert refusal is not None
    body = json.loads(refusal[0].text)
    assert body["error"] in {"service_unavailable", "internal_error"}
    assert "correlation_id" in body


@pytest.mark.asyncio
async def test_only_a_workspace_row_or_an_id_is_accepted() -> None:
    """Nothing is guessed from attributes: another object is a programming
    error (in enforce), and a row without its owner has the owner read."""
    from types import SimpleNamespace

    db = _db()
    wid = uuid4()
    with _mode("enforce"), _suspended(wid):
        with pytest.raises(TypeError):
            await gate.ensure_slot_active(
                db, SimpleNamespace(id=wid, owner_user_id=OWNER), user_id=OWNER
            )
        with pytest.raises(TypeError):
            await gate.slot_suspended(db, SimpleNamespace(id=wid))
        row = Workspace(id=wid, name="w", plan_name="free")  # owner not loaded
        db.scalar = AsyncMock(return_value=OWNER)
        await gate.ensure_slot_active(db, row, user_id=OWNER, policy=SlotPolicy.OWNER_MANAGEMENT)
        db.scalar.assert_awaited_once()


@pytest.mark.asyncio
async def test_revocation_never_refuses_and_runs_no_statement() -> None:
    db = _db()
    ws = _workspace()
    lookup = AsyncMock(return_value=frozenset({ws.id}))
    with _mode("enforce"), patch("services.workspace_slot_gate.suspended_workspace_ids", lookup):
        for user in (OWNER, "member-1", None):
            await gate.ensure_slot_active(db, ws, user_id=user, policy=SlotPolicy.REVOCATION)
    lookup.assert_not_awaited()


def test_an_outsider_on_mcp_learns_nothing_more() -> None:
    from mcp_server.tools._errors import describe_tool_exception

    body = json.loads(
        describe_tool_exception("recall", WorkspaceSlotSuspendedError.for_outsider())
        .response()[0]
        .text
    )
    assert body["help"] == "The workspace that holds this content is suspended."
    assert set(body) == {"status", "error", "message", "gate", "help"}
