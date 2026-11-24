"""recall with ``order="date"`` shows a superseded row, labelled, on Postgres (#3004).

A fact and its update are written through ``MemoryService.remember()``, the
update with ``supersedes=`` (the declared edge that shadows the old fact out of
a default recall). ``POST /memory/recall`` with ``order="date"`` — and no
``include_superseded`` — returns both, the old one first and labelled
``superseded_by``; a default recall still hides it, and an explicit
``include_superseded=false`` keeps it hidden under the date order too.

Stubbed: the search (``search_service.hybrid_search``, the update ranked first),
the embedding task and the Redis quota. The permission check, the shadow map,
the hydration and the route run on the database.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from api.routes.memory import recall
from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory
from models.schemas import RecallRequest, RememberRequest
from services.memory_service import MemoryService
from services.quota_service import DailyReservation


@pytest.fixture
def quiet_write():
    """No embedding task, no Redis quota: the parts of remember() not under test."""
    quota = MagicMock(
        check_memory_quota=AsyncMock(return_value=(True, None)),
        check_memories_per_day=AsyncMock(return_value=None),
        reserve_memories_per_day=AsyncMock(
            return_value=DailyReservation(key="quota:test:memories:day", count=1)
        ),
        release_memories_per_day=AsyncMock(return_value=None),
    )
    with (
        patch("services.memory_service.process_pending_embedding", new=AsyncMock()),
        patch("services.quota_service.QuotaService", return_value=quota),
    ):
        yield quota


async def _scope(db) -> tuple[str, Workspace, Context]:
    owner = f"order_{uuid4().hex[:8]}"
    db.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"order-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"order-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    db.add(ctx)
    await db.flush()
    await db.commit()
    return owner, ws, ctx


async def _remember(db, owner, ws, ctx, summary: str, **kw) -> UUID:
    result = await MemoryService(db).remember(
        RememberRequest(summary=summary, content=summary, type="fact", importance=0.6, **kw),
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    return result.memory_id


async def _recall(db, owner, ws, ctx, pool: list[UUID], **request_kw):
    svc = MemoryService(db)
    rows = [{"id": str(memory_id), "score": 0.9 - 0.1 * i} for i, memory_id in enumerate(pool)]
    svc.search_service.hybrid_search = AsyncMock(return_value=rows)
    request = RecallRequest(
        query="Where does the team meet?", k=5, filters={"context_id": str(ctx.id)}, **request_kw
    )
    return await recall(
        request=request,
        user={"user_id": owner, "current_workspace_id": ws.id},
        memory_service=svc,
        db=db,
    )


@pytest.mark.asyncio
async def test_date_order_returns_the_superseded_row_first_and_labelled(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    old = await _remember(db_session, owner, ws, ctx, "The team meets on Mondays")
    new = await _remember(
        db_session,
        owner,
        ws,
        ctx,
        "The team meets on Tuesdays from now on",
        supersedes=old,
        observed_at="2026-10-05",
    )
    # Written a month before the update's stated day, whatever today is.
    await db_session.execute(
        update(Memory).where(Memory.id == old).values(created_at=datetime(2026, 9, 1, 22, 0))
    )
    await db_session.commit()
    # The hydration reads the stored created_at, not the session's copy.
    db_session.expire_all()
    await db_session.refresh(ws)
    await db_session.refresh(ctx)
    pool = [new, old]  # the update ranks first

    default = await _recall(db_session, owner, ws, ctx, pool)
    assert [r.memory_id for r in default.results] == [new]
    assert default.order is None

    dated = await _recall(db_session, owner, ws, ctx, pool, order="date")
    assert dated.order == "date"
    assert [r.memory_id for r in dated.results] == [old, new]
    first, second = dated.results
    assert first.superseded_by == new
    assert first.date_source == "created"
    assert first.date == "2026-09-01"
    assert (second.date, second.date_source) == ("2026-10-05", "observed")
    assert second.superseded_by is None

    hidden = await _recall(db_session, owner, ws, ctx, pool, order="date", include_superseded=False)
    assert [r.memory_id for r in hidden.results] == [new]
    assert hidden.order == "date"
