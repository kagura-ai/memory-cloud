"""Workspace boundary regression test for memory-creation timeline (Issue #822).

``WorkspaceService.get_workspace_memory_timeline`` used to scope its daily
counts by *workspace membership* only — ``Memory.user_id.in_(member_ids)``
with no workspace/context filter. A user who belongs to multiple workspaces
therefore had *all* their memories (created in any workspace) counted in
*every* workspace they were a member of — a cross-tenant boundary leak of
memory-creation activity volume (the dashboard "Memory creation timeline"
chart).

This is the same defect family as #389/#391, #660/#662, and #435 (the
``Memory.user_id`` legacy-scoping pattern). The fix scopes the timeline to
the workspace's own contexts, matching the stats-card semantics in
``api/routes/workspace.py`` (``Context.workspace_id``).

The load-bearing predicate uses the cross-rooted-memory pattern (#614/#383):
a single ``multi_user`` is a member of both ``ws_a`` and ``ws_b`` and creates
memories in each. ``ws_a``'s timeline must count only the ``ws_a`` memory —
never the one rooted in ``ws_b``.
"""

from uuid import uuid4

import pytest
import pytest_asyncio

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, Workspace, WorkspaceMember
from models.memory import Memory
from services.workspace_service import WorkspaceService
from utils.datetime import utcnow


def _make_workspace(owner_id: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )


def _make_context(workspace_id, created_by: str) -> Context:
    return Context(
        id=uuid4(),
        workspace_id=workspace_id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=created_by,
    )


def _make_memory(user_id: str, workspace_id, context_id) -> Memory:
    return Memory(
        id=uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        context_id=context_id,
        summary=f"mem-{uuid4().hex[:6]}",
        content="x",
        type="note",
        client="test",
        tags=[],
        created_at=utcnow(),
    )


@pytest_asyncio.fixture
async def cross_rooted_scenario(db_session):
    """Two workspaces sharing one member who has a memory rooted in each.

    Returns ``(ws_a_id, ws_b_id, ctx_b_id)``. ``multi_user`` is a member of
    both workspaces and has exactly one memory in ``ws_a`` (via ``ctx_a``)
    and one in ``ws_b`` (via ``ctx_b``).
    """
    owner_a = f"owner_a_{uuid4().hex[:8]}"
    owner_b = f"owner_b_{uuid4().hex[:8]}"
    multi_user = f"multi_{uuid4().hex[:8]}"

    ws_a = _make_workspace(owner_a)
    ws_b = _make_workspace(owner_b)
    db_session.add_all([ws_a, ws_b])
    await db_session.flush()

    # multi_user is a member of BOTH workspaces — this is what made the old
    # member-scoped query leak ws_b activity into ws_a's timeline.
    db_session.add_all(
        [
            WorkspaceMember(
                workspace_id=ws_a.id,
                user_id=multi_user,
                role=WorkspaceRole.MEMBER,
            ),
            WorkspaceMember(
                workspace_id=ws_b.id,
                user_id=multi_user,
                role=WorkspaceRole.MEMBER,
            ),
        ]
    )

    ctx_a = _make_context(ws_a.id, multi_user)
    ctx_b = _make_context(ws_b.id, multi_user)
    db_session.add_all([ctx_a, ctx_b])
    await db_session.flush()

    db_session.add_all(
        [
            _make_memory(multi_user, ws_a.id, ctx_a.id),  # belongs to ws_a
            _make_memory(multi_user, ws_b.id, ctx_b.id),  # belongs to ws_b — must NOT leak
        ]
    )
    await db_session.commit()

    return ws_a.id, ws_b.id, ctx_b.id


@pytest.mark.asyncio
async def test_timeline_excludes_other_workspace_memories(cross_rooted_scenario, db_session):
    """ws_a's timeline counts only ws_a memories, not the shared member's ws_b memory."""
    ws_a_id, _ws_b_id, _ctx_b_id = cross_rooted_scenario

    service = WorkspaceService(db_session)
    result = await service.get_workspace_memory_timeline(ws_a_id, days=30)

    # Exactly one memory is rooted in ws_a. The pre-fix code counted 2
    # (it also picked up the ws_b memory because multi_user is a ws_a member).
    assert result["memories_created_in_period"] == 1


@pytest.mark.asyncio
async def test_timeline_context_filter_rejects_foreign_context(cross_rooted_scenario, db_session):
    """A context_id from another workspace must not surface its memories via ws_a's timeline."""
    ws_a_id, _ws_b_id, ctx_b_id = cross_rooted_scenario

    service = WorkspaceService(db_session)
    result = await service.get_workspace_memory_timeline(ws_a_id, days=30, context_id=ctx_b_id)

    # ctx_b does not belong to ws_a — filtering by it must yield no data,
    # not the ws_b memory it actually contains.
    assert result["memories_created_in_period"] == 0
