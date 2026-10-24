"""#1954: the capacity-over lock after authorization, on real rows.

``tests/mcp_server/test_capacity_resolver_gate.py`` proves the split with the
resolvers stubbed. Here every resolver-gated tool runs through
``execute_tool_call`` with the real handlers, ``ContextService`` /
``PermissionService`` and the real lock queries, against a Free workspace that
came from billing and holds more memories than its (patched, small) limit:

* the owner naming one of its contexts gets the same ``capacity_locked``
  envelope the dispatcher used to build, byte for byte;
* a member who may not read the context (a private one it did not create)
  and a non-member get the handler's not-found (or update_search_config's
  permission_denied) — never the lock or the workspace's numbers (D1);
* a member without a plan seat gets ``feature_not_available`` (#1943);
* the same call on a paid workspace is not refused;
* a handler called outside the dispatcher fails closed.

Only the rate limit (Redis / usage counting) is stubbed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.workspace_roles import WorkspaceRole
from mcp_server.tools._capacity_gate import (
    CAPACITY_LOCK_RESOLVER_GATED_TOOLS,
    invalidate_capacity_candidate_cache,
)
from mcp_server.tools._errors import describe_tool_exception
from models.auth import (
    ENTITLEMENT_SOURCE_EXTERNAL_BILLING,
    Context,
    User,
    Workspace,
    WorkspaceMember,
)
from models.memory import Memory
from services.capacity_lock import capacity_lock_state

MEMORY_LIMIT = 2


def _user(user_id: str, workspace_id) -> User:
    return User(
        email=f"{user_id}@example.test",
        user_id=user_id,
        name=user_id,
        role="user",
        is_initial_admin=False,
        auth_method="oauth",
        auth_provider="google",
        current_workspace_id=workspace_id,
    )


def _workspace(name: str, owner_id: str, plan_name: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=name,
        plan_name=plan_name,
        owner_user_id=owner_id,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )


def _context(ws, owner_id: str, name: str, *, private: bool = False) -> Context:
    return Context(
        id=uuid4(), workspace_id=ws.id, name=name, created_by=owner_id, is_private=private
    )


def _memory(ws, ctx, owner_id: str) -> Memory:
    return Memory(
        id=uuid4(),
        user_id=owner_id,
        workspace_id=ws.id,
        context_id=ctx.id,
        summary=f"mem-{uuid4().hex[:6]}",
        content="x",
        type="note",
        client="test",
        scope="working",
    )


@pytest.fixture
def limits():
    """Small memory limit; ``seats`` is mutable so a test can take a member's seat."""
    seats = {"n": 5}
    with (
        patch.object(Workspace, "effective_memory_limit", property(lambda self: MEMORY_LIMIT)),
        patch.object(Workspace, "effective_max_members", property(lambda self: seats["n"])),
    ):
        yield seats


@pytest_asyncio.fixture(loop_scope="session")
async def scenario(async_engine, db_session, monkeypatch, limits):
    """Locked workspace W (owner, a whitelisted member) with a shared, a second
    and a private context of the owner's, and a paid workspace of an outsider."""
    import db.base as _db_base

    monkeypatch.setattr(
        _db_base,
        "async_session_factory",
        async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False),
    )
    invalidate_capacity_candidate_cache()

    tag = uuid4().hex[:8]
    owner_id, member_id, outsider_id = f"cr-owner-{tag}", f"cr-member-{tag}", f"cr-out-{tag}"
    ws = _workspace(f"ws-cr-{tag}", owner_id, "free")
    ws.entitlement_source = ENTITLEMENT_SOURCE_EXTERNAL_BILLING
    other_ws = _workspace(f"ws-cr-other-{tag}", outsider_id, "pro")
    db_session.add_all([ws, other_ws])
    await db_session.flush()
    db_session.add_all(
        [_user(owner_id, ws.id), _user(member_id, ws.id), _user(outsider_id, other_ws.id)]
    )
    await db_session.flush()

    shared = _context(ws, owner_id, f"cr-shared-{tag}")
    second = _context(ws, owner_id, f"cr-second-{tag}")
    private = _context(ws, owner_id, f"cr-private-{tag}", private=True)
    db_session.add_all([shared, second, private])
    await db_session.flush()
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws.id, user_id=owner_id, role=WorkspaceRole.OWNER),
            WorkspaceMember(
                workspace_id=ws.id,
                user_id=member_id,
                role=WorkspaceRole.MEMBER,
                allowed_context_ids=[shared.id, second.id, private.id],
            ),
            WorkspaceMember(
                workspace_id=other_ws.id, user_id=outsider_id, role=WorkspaceRole.OWNER
            ),
        ]
    )
    db_session.add_all([_memory(ws, shared, owner_id) for _ in range(MEMORY_LIMIT + 1)])
    await db_session.commit()

    lock = await capacity_lock_state(db_session, ws)
    assert lock is not None and lock.over_memories == 1

    yield {
        "ws": ws,
        "other_ws": other_ws,
        "owner_id": owner_id,
        "member_id": member_id,
        "outsider_id": outsider_id,
        "shared": shared,
        "second": second,
        "private": private,
        "lock": lock,
    }

    ws_ids = [ws.id, other_ws.id]
    user_ids = [owner_id, member_id, outsider_id]
    try:
        await db_session.rollback()
        await db_session.execute(
            text("DELETE FROM memories WHERE workspace_id = ANY(:ids)"), {"ids": ws_ids}
        )
        await db_session.execute(
            text("DELETE FROM usage_stats WHERE user_id = ANY(:ids)"), {"ids": user_ids}
        )
        await db_session.execute(
            WorkspaceMember.__table__.delete().where(WorkspaceMember.workspace_id.in_(ws_ids))
        )
        await db_session.execute(Context.__table__.delete().where(Context.workspace_id.in_(ws_ids)))
        await db_session.execute(User.__table__.delete().where(User.user_id.in_(user_ids)))
        await db_session.execute(Workspace.__table__.delete().where(Workspace.id.in_(ws_ids)))
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise
    invalidate_capacity_candidate_cache()


def _args(tool: str, ctx, second) -> dict:
    """Valid arguments naming ``ctx`` (merge_contexts: ``ctx`` into ``second``)."""
    c = str(ctx.id)
    return {
        "bootstrap": {"context_id": c},
        "changes_since": {"context_id": c, "since": "2026-01-01T00:00:00Z"},
        "create_edge": {"context_id": c, "source_id": str(uuid4()), "target_id": str(uuid4())},
        "feedback": {"context_id": c, "memory_id": str(uuid4()), "helpful": True},
        "get_sleep_history": {"context_id": c},
        "get_state": {"context_id": c},
        "list_edges": {"context_id": c, "memory_id": str(uuid4())},
        "merge_contexts": {"source_context_id": c, "target_context_id": str(second.id)},
        "recall_nearby": {"context_id": c, "lat": 35.0, "lon": 139.0},
        "recall_series": {"context_id": c, "metric": "weight"},
        "recall_upcoming": {"context_id": c},
        "record_measurement": {"context_id": c, "metric": "weight", "value": 1.5},
        "rollback_session": {"context_id": c, "session_id": "s-1", "dry_run": True},
        "set_state": {"context_id": c, "key": "k", "value": "v"},
        "update_edge": {
            "context_id": c,
            "source_id": str(uuid4()),
            "target_id": str(uuid4()),
            "weight": 0.5,
        },
        "update_search_config": {"context_id": c, "semantic_weight": 0.5},
    }[tool]


async def _call(tool: str, args: dict, user_id: str, workspace_id):
    from mcp_server.tools import execute_tool_call

    with patch("mcp_server.tools._check_rate_limit", AsyncMock(return_value=(True, 0, 1000))):
        return await execute_tool_call(tool, args, user_id, workspace_id)


TOOLS = sorted(CAPACITY_LOCK_RESOLVER_GATED_TOOLS)


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("tool", TOOLS)
async def test_the_owner_gets_the_same_lock_envelope(scenario, tool) -> None:
    s = scenario
    result = await _call(tool, _args(tool, s["shared"], s["second"]), s["owner_id"], s["ws"].id)
    expected = describe_tool_exception(tool, s["lock"].to_error()).response()
    assert result[0].text == expected[0].text, result[0].text
    body = json.loads(result[0].text)
    assert body["error"] == "capacity_locked"
    assert (body["memory_count"], body["over_memories"]) == (MEMORY_LIMIT + 1, 1)


def _turned_away(tool: str) -> str:
    return "permission_denied" if tool == "update_search_config" else "context_not_found"


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("tool", TOOLS)
async def test_a_member_who_may_not_read_the_context_gets_not_found(scenario, tool) -> None:
    """A private context the member did not create: the lock used to confirm
    the id and show W's numbers."""
    s = scenario
    result = await _call(tool, _args(tool, s["private"], s["second"]), s["member_id"], s["ws"].id)
    body = json.loads(result[0].text)
    assert body["error"] == _turned_away(tool), body
    assert "over_memories" not in body and "cleanup_url" not in body


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("tool", TOOLS)
async def test_a_non_member_gets_not_found(scenario, tool) -> None:
    s = scenario
    result = await _call(
        tool, _args(tool, s["shared"], s["second"]), s["outsider_id"], s["other_ws"].id
    )
    body = json.loads(result[0].text)
    assert body["error"] == _turned_away(tool), body
    assert "over_memories" not in body and "cleanup_url" not in body


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize("tool", ["get_state", "create_edge"])
async def test_a_member_without_a_seat_is_refused_by_the_seat_check(scenario, limits, tool):
    s = scenario
    limits["n"] = 1  # the owner's seat only
    result = await _call(tool, _args(tool, s["shared"], s["second"]), s["member_id"], s["ws"].id)
    body = json.loads(result[0].text)
    assert body["error"] == "feature_not_available", body


@pytest.mark.asyncio(loop_scope="session")
async def test_a_paid_workspace_is_not_refused(scenario, db_session) -> None:
    s = scenario
    ws = await db_session.get(Workspace, s["ws"].id)
    ws.plan_name = "pro"
    await db_session.commit()
    result = await _call(
        "get_state", {"context_id": str(s["shared"].id)}, s["owner_id"], s["ws"].id
    )
    assert json.loads(result[0].text)["status"] == "success", result[0].text


@pytest.mark.asyncio(loop_scope="session")
async def test_a_handler_called_outside_the_dispatcher_fails_closed(scenario) -> None:
    from mcp_server.tools._helpers import _mcp_capacity_policy
    from mcp_server.tools.state import handle_get_state

    s = scenario
    assert _mcp_capacity_policy.get() is None
    try:
        result = await handle_get_state(
            {"context_id": str(s["shared"].id)}, s["owner_id"], s["ws"].id
        )
        error = json.loads(result[0].text)["error"]
    except Exception as exc:  # noqa: BLE001 — a raising handler is mapped by the dispatcher
        error = type(exc).__name__
    assert error in ("capacity_locked", "CapacityLockedError")
