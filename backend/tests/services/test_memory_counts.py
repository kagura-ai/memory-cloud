"""``services.memory_counts`` — the one definition of a workspace's memory count (#1946).

Every ``memory_limit`` comparison (the remember quota gate, the plan page, the
workspace stats card, the downgrade check, the capacity lock, the resource
indexer) must count the same rows: memories whose ``workspace_id`` is this
workspace and that are not soft-deleted. The legacy member-``user_id`` count
double-counted a member's memories from their other workspaces and kept
counting rows whose owner had left.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from models.auth import Workspace
from models.memory import Memory
from services.memory_counts import (
    count_live_memories,
    live_memory_count_stmt,
    live_memory_counts_by_workspace,
)
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio


async def _workspace(db) -> Workspace:
    ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        owner_user_id=f"owner-{uuid4().hex[:8]}",
        plan_name="free",
    )
    db.add(ws)
    await db.flush()
    return ws


async def _memory(db, user_id, workspace_id, **kwargs) -> Memory:
    mem = Memory(
        id=uuid4(),
        user_id=user_id,
        workspace_id=workspace_id,
        summary="s",
        content="c",
        type="code",
        client="pytest",
        **kwargs,
    )
    db.add(mem)
    await db.flush()
    return mem


async def test_counts_only_this_workspaces_live_rows(db_session):
    ws_a = await _workspace(db_session)
    ws_b = await _workspace(db_session)
    member = f"member-{uuid4().hex[:8]}"
    # Two live rows in A, by two different users (one of whom never joined A —
    # the count is by workspace, not by membership).
    await _memory(db_session, member, ws_a.id)
    await _memory(db_session, f"departed-{uuid4().hex[:8]}", ws_a.id)
    # Noise that must not count toward A: the same member's row in B and a
    # soft-deleted row in A. (A row without a workspace cannot exist since
    # e95 / #1951 made the column NOT NULL.)
    await _memory(db_session, member, ws_b.id)
    await _memory(db_session, member, ws_a.id, deleted_at=utcnow())

    assert await count_live_memories(db_session, ws_a.id) == 2
    assert await count_live_memories(db_session, ws_b.id) == 1


async def test_empty_workspace_counts_zero(db_session):
    ws = await _workspace(db_session)
    assert await count_live_memories(db_session, ws.id) == 0


async def test_statement_matches_the_helper(db_session):
    """Callers that batch their own scalar reads use the statement directly."""
    ws = await _workspace(db_session)
    await _memory(db_session, "u", ws.id)
    await _memory(db_session, "u", ws.id, deleted_at=utcnow())

    assert (await db_session.execute(live_memory_count_stmt(ws.id))).scalar() == 1
    assert await count_live_memories(db_session, ws.id) == 1


async def test_grouped_counts_match_the_single_count(db_session):
    """The admin list reads many workspaces at once; same predicate, one query,
    and a workspace without live rows is simply absent."""
    ws_a = await _workspace(db_session)
    ws_b = await _workspace(db_session)
    ws_empty = await _workspace(db_session)
    await _memory(db_session, "u", ws_a.id)
    await _memory(db_session, "v", ws_a.id)
    await _memory(db_session, "u", ws_a.id, deleted_at=utcnow())
    await _memory(db_session, "u", ws_b.id)

    counts = await live_memory_counts_by_workspace(db_session, [ws_a.id, ws_b.id, ws_empty.id])

    assert counts == {ws_a.id: 2, ws_b.id: 1}
    assert counts.get(ws_a.id) == await count_live_memories(db_session, ws_a.id)
    assert await live_memory_counts_by_workspace(db_session, []) == {}
