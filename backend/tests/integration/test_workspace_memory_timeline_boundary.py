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
the workspace's own contexts (``Context.workspace_id``), matching the
context-scoped stats path in ``api/routes/workspace.py``.

The fixture seeds memories chosen so that each assertion is *load-bearing* for
a specific scoping choice — a regression to a different-but-plausible scope
would change the count and fail the test:

  * ``m_member``   — member-authored, ``Memory.workspace_id == ws_a``: the
                     baseline ws_a memory.
  * ``m_outsider`` — authored by a NON-member, rooted in a ws_a context: must
                     be counted. Catches a regression that re-adds the dropped
                     ``Memory.user_id.in_(member_ids)`` filter (#822 finding 2).
  * ``m_null_ws``  — ``Memory.workspace_id IS NULL``, rooted in a ws_a context:
                     must be counted. Catches a regression that scopes by the
                     nullable ``Memory.workspace_id`` instead of
                     ``Context.workspace_id`` (#822 finding 1).
  * ``m_deleted_ctx`` — rooted in a *soft-deleted* ws_a context: must NOT be
                     counted. Catches dropping ``Context.deleted_at.is_(None)``.
  * ``m_other_ws`` — the cross-rooted leak case (#614/#383): authored by
                     ``multi_user`` (a member of ws_a) but rooted in ws_b. Must
                     NOT appear in ws_a's timeline.

So ws_a's timeline must count exactly the 3 live ws_a-context memories.
"""

from uuid import uuid4

import pytest
import pytest_asyncio

from auth.workspace_roles import WorkspaceRole
from models.auth import Context, Workspace, WorkspaceMember
from models.memory import Memory
from services.workspace_service import WorkspaceService
from utils.datetime import utcnow

EXPECTED_WS_A_COUNT = 3
TIMELINE_DAYS = 30


def _make_workspace(owner_id: str) -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_id,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )


def _make_context(workspace_id, created_by: str, *, deleted: bool = False) -> Context:
    return Context(
        id=uuid4(),
        workspace_id=workspace_id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=created_by,
        deleted_at=utcnow() if deleted else None,
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
    """Seed two workspaces; ws_a gets memories that isolate each scoping choice.

    Returns ``(ws_a_id, ws_b_id, ctx_b_id)``. See the module docstring for the
    role each seeded memory plays.
    """
    owner_a = f"owner_a_{uuid4().hex[:8]}"
    owner_b = f"owner_b_{uuid4().hex[:8]}"
    multi_user = f"multi_{uuid4().hex[:8]}"
    outsider = f"outsider_{uuid4().hex[:8]}"  # NOT a member of either workspace

    ws_a = _make_workspace(owner_a)
    ws_b = _make_workspace(owner_b)
    db_session.add_all([ws_a, ws_b])
    await db_session.flush()

    # multi_user is a member of BOTH workspaces — this is what made the old
    # member-scoped query leak ws_b activity into ws_a's timeline.
    db_session.add_all(
        [
            WorkspaceMember(workspace_id=ws_a.id, user_id=multi_user, role=WorkspaceRole.MEMBER),
            WorkspaceMember(workspace_id=ws_b.id, user_id=multi_user, role=WorkspaceRole.MEMBER),
        ]
    )

    ctx_a = _make_context(ws_a.id, multi_user)
    ctx_a_deleted = _make_context(ws_a.id, multi_user, deleted=True)
    ctx_b = _make_context(ws_b.id, multi_user)
    db_session.add_all([ctx_a, ctx_a_deleted, ctx_b])
    await db_session.flush()

    db_session.add_all(
        [
            _make_memory(multi_user, ws_a.id, ctx_a.id),  # m_member   → counts
            _make_memory(outsider, ws_a.id, ctx_a.id),  # m_outsider → counts (no member filter)
            _make_memory(multi_user, None, ctx_a.id),  # m_null_ws  → counts (ctx scope, not ws_id)
            _make_memory(multi_user, ws_a.id, ctx_a_deleted.id),  # m_deleted_ctx → excluded
            _make_memory(multi_user, ws_b.id, ctx_b.id),  # m_other_ws → must NOT leak
        ]
    )
    await db_session.commit()

    return ws_a.id, ws_b.id, ctx_b.id


@pytest.mark.asyncio
async def test_timeline_counts_only_live_workspace_contexts(cross_rooted_scenario, db_session):
    """ws_a's timeline counts exactly its 3 live ws_a-context memories.

    The single count of 3 (not 2, not 4, not 5) simultaneously pins: the
    cross-workspace memory is excluded, the non-member memory is included, the
    NULL-``Memory.workspace_id`` memory is included, and the soft-deleted
    context's memory is excluded. A regression to membership scoping,
    ``Memory.workspace_id`` scoping, or dropping the deleted-context filter all
    move this number.
    """
    ws_a_id, _ws_b_id, _ctx_b_id = cross_rooted_scenario

    service = WorkspaceService(db_session)
    result = await service.get_workspace_memory_timeline(ws_a_id, days=TIMELINE_DAYS)

    assert result["memories_created_in_period"] == EXPECTED_WS_A_COUNT


@pytest.mark.asyncio
async def test_timeline_returns_zero_filled_window(cross_rooted_scenario, db_session):
    """The daily_counts payload is a continuous, zero-filled window of TIMELINE_DAYS days."""
    ws_a_id, _ws_b_id, _ctx_b_id = cross_rooted_scenario

    service = WorkspaceService(db_session)
    result = await service.get_workspace_memory_timeline(ws_a_id, days=TIMELINE_DAYS)

    daily = result["daily_counts"]
    # Continuous zero-filled range — the frontend chart depends on every day
    # being present, not just days that had activity.
    assert len(daily) == TIMELINE_DAYS
    assert [d["date"] for d in daily] == sorted(d["date"] for d in daily)
    assert sum(d["count"] for d in daily) == EXPECTED_WS_A_COUNT
    # All three live memories were created today → bucketed into the last day.
    assert daily[-1]["count"] == EXPECTED_WS_A_COUNT
    assert daily[-1]["date"] == result["period_end"]


@pytest.mark.asyncio
async def test_timeline_context_filter_rejects_foreign_context(cross_rooted_scenario, db_session):
    """A context_id from another workspace must not surface its memories via ws_a's timeline."""
    ws_a_id, _ws_b_id, ctx_b_id = cross_rooted_scenario

    service = WorkspaceService(db_session)
    result = await service.get_workspace_memory_timeline(
        ws_a_id, days=TIMELINE_DAYS, context_id=ctx_b_id
    )

    # ctx_b does not belong to ws_a — filtering by it must yield no data,
    # not the ws_b memory it actually contains.
    assert result["memories_created_in_period"] == 0
    assert len(result["daily_counts"]) == TIMELINE_DAYS  # still a full zero-filled window
