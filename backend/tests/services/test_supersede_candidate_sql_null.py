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


async def _set_raw(db, memory_id: UUID, value: str | None) -> None:
    """Write the column bypassing the ORM (a concurrent writer, or legacy JSON null)."""
    await db.execute(
        text("UPDATE memories SET supersede_candidate = CAST(:v AS json) WHERE id = :id"),
        {"v": value, "id": memory_id},
    )
    await db.commit()


async def _target_of(db, memory_id: UUID) -> str | None:
    return (
        await db.execute(
            text("SELECT supersede_candidate ->> 'memory_id' FROM memories WHERE id = :id"),
            {"id": memory_id},
        )
    ).scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize("seen", [None, "null", '{"dismissed": {"memory_id": "x"}}'])
async def test_detection_write_applies_over_what_the_re_read_saw(db_session, quiet_write, seen):
    """#2069: the conditional detection write matches SQL NULL, the JSON null
    the #2022 walk may have skipped, and a stored value the re-read saw —
    compared as JSONB, since the column is ``json``."""
    from services.memory_service import _store_supersede_candidate

    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    memory_id = await _remember(db_session, owner, ws, ctx)
    await _set_raw(db_session, memory_id, seen)
    memory = (await db_session.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()
    await db_session.refresh(memory, attribute_names=["supersede_candidate"])
    target = str(uuid4())

    written = await _store_supersede_candidate(
        db_session, memory=memory, candidate={"memory_id": target, "similarity": 0.93}
    )

    assert written is True
    assert await _target_of(db_session, memory_id) == target


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seen", "meanwhile"),
    [
        (None, '{"dismissed": {"memory_id": "x", "dismissed_at": "t"}}'),
        ('{"memory_id": "a", "similarity": 0.9}', None),
        ('{"memory_id": "a", "similarity": 0.9}', '{"dismissed": {"memory_id": "a"}}'),
        ('{"dismissed": {"memory_id": "b"}}', '{"dismissed": {"memory_id": "*"}}'),
    ],
)
async def test_detection_write_skips_a_row_changed_after_the_re_read(
    db_session, quiet_write, seen, meanwhile
):
    """A dismissal, an accept's clear or another write committed after the
    re-read is never overwritten: the UPDATE matches nothing."""
    from services.memory_service import _store_supersede_candidate

    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    memory_id = await _remember(db_session, owner, ws, ctx)
    await _set_raw(db_session, memory_id, seen)
    memory = (await db_session.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()
    await db_session.refresh(memory, attribute_names=["supersede_candidate"])
    await _set_raw(db_session, memory_id, meanwhile)

    written = await _store_supersede_candidate(
        db_session, memory=memory, candidate={"memory_id": str(uuid4()), "similarity": 0.93}
    )

    assert written is False
    stored = (
        await db_session.execute(
            text("SELECT supersede_candidate::text FROM memories WHERE id = :id"),
            {"id": memory_id},
        )
    ).scalar_one()
    assert stored == meanwhile


@pytest.mark.asyncio
@pytest.mark.parametrize("meanwhile", ["edit", "accept", "forget"])
async def test_detection_write_skips_an_edit_accept_or_delete_after_the_re_read(
    db_session, quiet_write, meanwhile
):
    """The column is untouched, but the text this run embedded changed (the
    edit's own run owns the column), the pair was accepted by an edge, or the
    memory was deleted: nothing is written."""
    from models.memory import EDGE_TYPE_SUPERSEDES, NeuralMemoryEdge
    from services.memory_service import _store_supersede_candidate

    owner = f"o-{uuid4().hex[:6]}"
    ws, ctx = await _scope(db_session, owner)
    await db_session.commit()
    target_id = await _remember(db_session, owner, ws, ctx, summary="the older fact")
    memory_id = await _remember(db_session, owner, ws, ctx)
    memory = (await db_session.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()
    if meanwhile == "edit":
        statement = "UPDATE memories SET content = 'edited content' WHERE id = :id"
        await db_session.execute(text(statement), {"id": memory_id})
    elif meanwhile == "forget":
        statement = "UPDATE memories SET deleted_at = now() WHERE id = :id"
        await db_session.execute(text(statement), {"id": memory_id})
    else:
        db_session.add(
            NeuralMemoryEdge(
                user_id=owner,
                workspace_id=ws.id,
                context_id=ctx.id,
                src_id=memory_id,
                dst_id=target_id,
                edge_type=EDGE_TYPE_SUPERSEDES,
                weight=1.0,
            )
        )
    await db_session.commit()

    written = await _store_supersede_candidate(
        db_session, memory=memory, candidate={"memory_id": str(target_id), "similarity": 0.93}
    )

    assert written is False
    assert await _stored(db_session, memory_id) == (True, None)
