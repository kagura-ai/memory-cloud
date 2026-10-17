"""``memories.supersede_candidate`` stores SQL ``NULL`` for "no candidate" (#2022).

The column is Postgres ``json``. With SQLAlchemy's default
``JSON(none_as_null=False)`` a Python ``None`` was written as the JSON value
``null``, so operator queries with ``supersede_candidate IS NULL`` counted almost
nothing. Reading through the ORM cannot tell the two apart (both load as
``None``), so every check here is raw SQL against the stored value.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory
from models.schemas import RememberRequest
from services.edge_service import accept_supersede_candidate_if_matching
from services.memory_service import MemoryService
from services.quota_service import DailyReservation


async def _scope(db, owner: str) -> tuple[Workspace, Context]:
    db.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"sc-ws-{uuid4().hex[:8]}",
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
        name=f"sc-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
        trust_tier="trusted",
    )
    db.add(ctx)
    await db.flush()
    return ws, ctx


@pytest.fixture
def quiet_write():
    """No embedding task, no Redis quota: the parts of remember() that are not under test."""
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


async def _stored(db, memory_id: UUID) -> tuple[bool, str | None]:
    """(``IS NULL``, ``json_typeof``) of the stored column, bypassing the ORM."""
    row = (
        await db.execute(
            text(
                "SELECT supersede_candidate IS NULL AS sql_null, "
                "json_typeof(supersede_candidate) AS json_type "
                "FROM memories WHERE id = :id"
            ),
            {"id": memory_id},
        )
    ).one()
    return row.sql_null, row.json_type


def _req(summary: str) -> RememberRequest:
    return RememberRequest(summary=summary, content="content", type="note", importance=0.5)


async def _remember(
    db, owner: str, ws: Workspace, ctx: Context, *, summary: str = "a remembered fact", **kw
) -> UUID:
    result = await MemoryService(db).remember(
        _req(summary),
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
        **kw,
    )
    return result.memory_id


@pytest.mark.asyncio
async def test_fresh_remember_leaves_sql_null(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()

    memory_id = await _remember(db_session, owner, ws, ctx)

    assert await _stored(db_session, memory_id) == (True, None)


@pytest.mark.asyncio
async def test_remember_many_leaves_sql_null(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()

    results = await MemoryService(db_session).remember_many(
        [_req("first remembered fact"), _req("second remembered fact")],
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )

    for result in results:
        assert await _stored(db_session, result.memory_id) == (True, None)


@pytest.mark.asyncio
async def test_dedupe_off_still_stores_the_tombstone_object(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()

    memory_id = await _remember(db_session, owner, ws, ctx, dedupe="off")

    assert await _stored(db_session, memory_id) == (False, "object")


@pytest.mark.asyncio
async def test_accepting_a_candidate_leaves_sql_null(db_session, quiet_write):
    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    old_id = await _remember(db_session, owner, ws, ctx, summary="the old fact")
    new_id = await _remember(db_session, owner, ws, ctx, summary="the new fact")
    new = (await db_session.execute(select(Memory).where(Memory.id == new_id))).scalar_one()
    new.supersede_candidate = {"memory_id": str(old_id), "summary": "old", "similarity": 0.95}
    await db_session.commit()
    assert await _stored(db_session, new_id) == (False, "object")

    await accept_supersede_candidate_if_matching(db_session, src_id=new_id, dst_id=old_id)
    await db_session.commit()

    assert await _stored(db_session, new_id) == (True, None)
