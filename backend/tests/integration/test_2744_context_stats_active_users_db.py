"""#2744: the dashboard's "Active users (7d)" is a distinct count, over 7 days.

``WorkspaceService.get_context_stats`` returns ``active_users_week`` per
context as ``COUNT(DISTINCT user_id)``. The KPI card used to add those up, so
a user active in N contexts counted N times; the workspace total is now its
own ``COUNT(DISTINCT user_id)`` over the same rows (window, context-scoped
calls, the contexts the caller can open).

The window is 7 calendar days: today and the 6 days before it. A row dated
7 days ago used to be counted (8 days under a 7-day label).

Seeded rows, workspace W with contexts c1, c2 and c3:

  * ``a``  — today in c1 and in c2: one user in two contexts.
  * ``b``  — 6 days ago in c1: the oldest day inside the window.
  * ``c``  — today in c3 only: outside ``context_ids`` when the caller is a
             member that cannot open c3.
  * ``d``  — 7 days ago in c2: outside the window.
  * ``e``  — today in a context of another workspace.
  * ``f``  — today in W without a context (not context-scoped).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from models.auth import Context, UsageStats, Workspace
from services.workspace_service import WorkspaceService
from utils.datetime import utcnow


def _workspace() -> Workspace:
    return Workspace(
        id=uuid4(),
        name=f"ws-2744-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=f"owner_{uuid4().hex[:8]}",
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )


def _context(workspace_id: UUID) -> Context:
    return Context(
        id=uuid4(),
        workspace_id=workspace_id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by="owner",
    )


def _usage(user_id: str, workspace_id: UUID, context_id: UUID | None, day: date) -> UsageStats:
    return UsageStats(
        user_id=user_id,
        endpoint="/api/v1/memory/recall",
        method="POST",
        status_code=200,
        response_time_ms=10,
        created_at=utcnow(),
        date=day,
        workspace_id=workspace_id,
        context_id=context_id,
    )


@dataclass
class Seeded:
    ws: UUID
    c1: UUID
    c2: UUID
    c3: UUID


@pytest_asyncio.fixture
async def seeded(db_session) -> Seeded:
    ws, other_ws = _workspace(), _workspace()
    db_session.add_all([ws, other_ws])
    await db_session.flush()
    c1, c2, c3 = _context(ws.id), _context(ws.id), _context(ws.id)
    foreign = _context(other_ws.id)
    db_session.add_all([c1, c2, c3, foreign])
    await db_session.flush()

    uid = uuid4().hex[:8]
    today = utcnow().date()
    db_session.add_all(
        [
            _usage(f"a_{uid}", ws.id, c1.id, today),
            _usage(f"a_{uid}", ws.id, c2.id, today),
            _usage(f"b_{uid}", ws.id, c1.id, today - timedelta(days=6)),
            _usage(f"c_{uid}", ws.id, c3.id, today),
            _usage(f"d_{uid}", ws.id, c2.id, today - timedelta(days=7)),
            _usage(f"e_{uid}", other_ws.id, foreign.id, today),
            _usage(f"f_{uid}", ws.id, None, today),
        ]
    )
    await db_session.commit()
    return Seeded(ws=ws.id, c1=c1.id, c2=c2.id, c3=c3.id)


def _by_context(stats: dict) -> dict[str, dict]:
    return {row["context_id"]: row for row in stats["contexts"]}


@pytest.mark.asyncio
async def test_a_member_total_counts_each_user_once_over_seven_days(seeded, db_session) -> None:
    """A member that opens c1 and c2: ``a`` (both contexts) and ``b`` (6 days
    ago) are the active users; ``c`` (c3), ``d`` (7 days ago), ``e`` (other
    workspace) and ``f`` (no context) are not."""
    stats = await WorkspaceService(db_session).get_context_stats(
        seeded.ws, context_ids={seeded.c1, seeded.c2}
    )

    rows = _by_context(stats)
    assert set(rows) == {str(seeded.c1), str(seeded.c2)}
    assert rows[str(seeded.c1)]["active_users_week"] == 2  # a, b
    assert rows[str(seeded.c1)]["api_calls_week"] == 2
    assert rows[str(seeded.c2)]["active_users_week"] == 1  # a; d is 7 days old
    assert rows[str(seeded.c2)]["api_calls_week"] == 1
    assert stats["workspace_totals"]["active_users_week"] == 2  # a once, b


@pytest.mark.asyncio
async def test_an_owner_total_covers_every_context(seeded, db_session) -> None:
    """With no ``context_ids`` (owner or admin) c3's user is counted too."""
    stats = await WorkspaceService(db_session).get_context_stats(seeded.ws)

    rows = _by_context(stats)
    assert rows[str(seeded.c3)]["active_users_week"] == 1
    assert stats["workspace_totals"]["active_users_week"] == 3  # a, b, c


@pytest.mark.asyncio
async def test_one_user_in_two_contexts_is_one_active_user(db_session) -> None:
    ws = _workspace()
    db_session.add(ws)
    await db_session.flush()
    c1, c2 = _context(ws.id), _context(ws.id)
    db_session.add_all([c1, c2])
    await db_session.flush()
    today = utcnow().date()
    db_session.add_all([_usage("solo", ws.id, c1.id, today), _usage("solo", ws.id, c2.id, today)])
    await db_session.commit()

    stats = await WorkspaceService(db_session).get_context_stats(ws.id)

    rows = _by_context(stats)
    assert rows[str(c1.id)]["active_users_week"] == 1
    assert rows[str(c2.id)]["active_users_week"] == 1
    assert stats["workspace_totals"]["active_users_week"] == 1


@pytest.mark.asyncio
async def test_no_usage_gives_a_zero_total(db_session) -> None:
    ws = _workspace()
    db_session.add(ws)
    await db_session.flush()
    db_session.add(_context(ws.id))
    await db_session.commit()

    stats = await WorkspaceService(db_session).get_context_stats(ws.id, context_ids=set())

    assert stats["contexts"] == []
    assert stats["workspace_totals"] == {"memory_count": 0, "active_users_week": 0}


@pytest.mark.asyncio
async def test_a_soft_deleted_context_is_left_out_of_the_total(seeded, db_session) -> None:
    """The total covers exactly the contexts the response lists: a usage row
    in a soft-deleted context counts neither as a row nor in the total, for
    an owner (no ``context_ids``) or a member whose list still names it."""
    gone = _context(seeded.ws)
    gone.deleted_at = utcnow()
    db_session.add(gone)
    await db_session.flush()
    db_session.add(_usage(f"g_{uuid4().hex[:8]}", seeded.ws, gone.id, utcnow().date()))
    await db_session.commit()

    owner = await WorkspaceService(db_session).get_context_stats(seeded.ws)
    assert str(gone.id) not in _by_context(owner)
    assert owner["workspace_totals"]["active_users_week"] == 3  # a, b, c; not g

    member = await WorkspaceService(db_session).get_context_stats(
        seeded.ws, context_ids={seeded.c1, gone.id}
    )
    assert set(_by_context(member)) == {str(seeded.c1)}
    assert member["workspace_totals"]["active_users_week"] == 2  # a, b; not g
