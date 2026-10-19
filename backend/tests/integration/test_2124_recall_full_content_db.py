"""recall(full=N) puts content on exactly the top N rows (#2124), on Postgres.

Real memories are written through ``MemoryService.remember()`` and read back by
``MemoryService.recall()``. Stubbed at the module boundary only: the embedding
task, the Redis quota and ``search_service.hybrid_search`` (the candidate pool
in a fixed order, with the cross-encoder on and off). Hydration, the page
builder and the content slice run on the database rows. A fold head's content
is pinned in ``test_2123_recall_fold_chain_db.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.schemas import RecallRequest, RememberRequest
from services.memory_service import MemoryService
from services.quota_service import DailyReservation
from utils.recall_projection import RECALL_CONTENT_CHARS


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
    owner = f"full_{uuid4().hex[:8]}"
    db.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"full-ws-{uuid4().hex[:8]}",
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
        name=f"full-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    db.add(ctx)
    await db.flush()
    await db.commit()
    return owner, ws, ctx


async def _remember(db, owner, ws, ctx, summary: str, content: str) -> UUID:
    result = await MemoryService(db).remember(
        RememberRequest(summary=summary, content=content, type="fact", importance=0.6),
        user_id=owner,
        client="pytest",
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )
    return result.memory_id


@pytest.mark.asyncio
@pytest.mark.parametrize("use_rerank", [True, False])
async def test_full_two_puts_content_on_exactly_the_top_two(db_session, quiet_write, use_rerank):
    owner, ws, ctx = await _scope(db_session)
    long_body = "Falcon release checklist. " * 100  # > RECALL_CONTENT_CHARS
    rows = [
        ("Falcon launch date", long_body),
        ("Falcon QA lead", "Mina leads QA for Falcon."),
        ("Falcon budget", "The Falcon budget is 40k."),
        ("Falcon venue", "Launch event at the main hall."),
    ]
    ids = [await _remember(db_session, owner, ws, ctx, s, c) for s, c in rows]
    # The cross-encoder and the hybrid order rank differently; the top N
    # follows whatever order the page ships in.
    pool = [ids[1], ids[0], ids[2], ids[3]] if use_rerank else ids
    contents = dict(zip(ids, (c for _, c in rows), strict=True))

    svc = MemoryService(db_session)
    svc.search_service.hybrid_search = AsyncMock(
        return_value=[{"id": str(m), "score": 0.9 - 0.1 * i} for i, m in enumerate(pool)]
    )
    response = await svc.recall(
        RecallRequest(query="Falcon", k=5, full=2, use_rerank=use_rerank),
        user_id=owner,
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
    )

    assert svc.search_service.hybrid_search.await_args.kwargs["use_rerank"] is use_rerank
    assert [r.memory_id for r in response.results] == pool
    top, rest = response.results[:2], response.results[2:]
    for r in top:
        full_text = contents[r.memory_id]
        assert r.content == full_text[:RECALL_CONTENT_CHARS]
        if len(full_text) > RECALL_CONTENT_CHARS:
            assert r.content_truncated is True
            assert r.content_next_offset == RECALL_CONTENT_CHARS
        else:
            assert r.content_truncated is None and r.content_next_offset is None
    assert all(r.content is None for r in rest)
    # The summaries are untouched on every row.
    assert {r.summary for r in response.results} == {s for s, _ in rows}


@pytest.mark.asyncio
async def test_service_default_and_zero_are_summaries_only_three_is_three(db_session, quiet_write):
    owner, ws, ctx = await _scope(db_session)
    ids = [
        await _remember(db_session, owner, ws, ctx, f"stored fact number {i}", f"body {i}")
        for i in range(4)
    ]

    async def recall(**kw):
        svc = MemoryService(db_session)
        svc.search_service.hybrid_search = AsyncMock(
            return_value=[{"id": str(m), "score": 0.9 - 0.1 * i} for i, m in enumerate(ids)]
        )
        return await svc.recall(
            RecallRequest(query="fact", k=5, **kw),
            user_id=owner,
            current_context_id=ctx.id,
            current_workspace_id=ws.id,
        )

    # The service (REST, internal callers) ships no content unless asked; the
    # MCP tool sends full=3 when its caller omits it.
    default = await recall()
    assert all(r.content is None for r in default.results)
    three = await recall(full=3)
    assert [r.content for r in three.results] == ["body 0", "body 1", "body 2", None]
    none = await recall(full=0)
    assert all(r.content is None for r in none.results)


@pytest.mark.asyncio
async def test_defaulted_full_inlines_only_trusted_rows(db_session, quiet_write):
    """The MCP tool's defaulted full (content_trusted_only) passes over a
    connector-ingested row — its slot goes to the next trusted row; an
    explicit full gives it content like any other row."""
    from sqlalchemy import update

    from models.memory import SOURCE_TYPE_CONNECTOR, Memory

    owner, ws, ctx = await _scope(db_session)
    ids = [
        await _remember(db_session, owner, ws, ctx, f"stored fact number {i}", f"body {i}")
        for i in range(5)
    ]
    await db_session.execute(
        update(Memory).where(Memory.id == ids[0]).values(source_type=SOURCE_TYPE_CONNECTOR)
    )
    await db_session.commit()

    async def recall(**kw):
        svc = MemoryService(db_session)
        svc.search_service.hybrid_search = AsyncMock(
            return_value=[{"id": str(m), "score": 0.9 - 0.1 * i} for i, m in enumerate(ids)]
        )
        return await svc.recall(
            RecallRequest(query="fact", k=5, full=3),
            user_id=owner,
            current_context_id=ctx.id,
            current_workspace_id=ws.id,
            **kw,
        )

    defaulted = await recall(content_trusted_only=True)
    assert [r.content for r in defaulted.results] == [None, "body 1", "body 2", "body 3", None]
    explicit = await recall()
    assert [r.content for r in explicit.results] == ["body 0", "body 1", "body 2", None, None]
