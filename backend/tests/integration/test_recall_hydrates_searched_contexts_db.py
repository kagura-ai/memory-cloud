"""recall hydrates only the rows of the contexts it searched, on Postgres.

A resource point's id comes from the document, so two live rows of different
contexts can name one point (``unshared_point_id`` says how). The point carries
one ``context_id``; a point two rows name resolves to the searched context's
row, and with several searched contexts to the row of the context the point
carries.

Real rows, read back by ``MemoryService.recall()``. ``hybrid_search`` is stubbed
with the point the vector store would return, so each test fixes which point
the search found; hydration, the page builder and the row filters run on the
database. Every test inserts the rows in both orders: hydration keys its result
by point id, so a row order must never pick the answer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import Memory
from models.schemas import RecallRequest, RememberRequest
from services.memory_service import MemoryService
from services.quota_service import DailyReservation
from services.resource_indexer import resource_point_id

BOTH_ORDERS = pytest.mark.parametrize("first_row_inserted_first", [True, False])


@pytest.fixture
def point() -> UUID:
    """A document version's point, of its own per test: committed rows outlive one."""
    return resource_point_id(f"slug-{uuid4().hex[:8]}", "doc-1", 1)


async def _workspace(db) -> tuple[str, Workspace]:
    owner = f"hydrate_{uuid4().hex[:8]}"
    db.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"hydrate-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    await db.flush()
    return owner, ws


async def _context(db, owner: str, ws: Workspace) -> Context:
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"hydrate-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    db.add(ctx)
    await db.flush()
    return ctx


async def _row_of_point(db, ctx: Context, point: UUID, summary: str) -> Memory:
    """A row the indexer would write for the document version of ``point``."""
    row = Memory(
        id=uuid4(),
        user_id=ctx.created_by,
        workspace_id=ctx.workspace_id,
        context_id=ctx.id,
        summary=summary,
        content="{}",
        details={"doc_id": "doc-1", "version": 1},
        context={"context_id": str(ctx.id)},
        type="resource_data",
        importance=0.6,
        scope="working",
        client="resource_indexer",
        source="resource_ingest",
        source_type="connector",
        tags=[],
        summary_embedding_id=point,
        embedding_status="success",
    )
    db.add(row)
    await db.flush()
    return row


async def _rows_in_order(
    db,
    first_context: Context,
    second_context: Context,
    point: UUID,
    *,
    first_row_inserted_first: bool,
) -> tuple[Memory, Memory]:
    if first_row_inserted_first:
        first_row = await _row_of_point(db, first_context, point, "row of the first context")
        second_row = await _row_of_point(db, second_context, point, "row of the second context")
    else:
        second_row = await _row_of_point(db, second_context, point, "row of the second context")
        first_row = await _row_of_point(db, first_context, point, "row of the first context")
    await db.commit()
    return first_row, second_row


def _point_hit(context: Context, point: UUID, score: float = 0.9) -> dict:
    """The hit the vector store returns: the point carries one ``context_id``."""
    return {"id": str(point), "score": score, "payload": {"context_id": str(context.id)}}


async def _recall(db, hits: list[dict], *, user_id: str, ws: Workspace, ctx: Context, **kwargs):
    svc = MemoryService(db)
    svc.search_service.hybrid_search = AsyncMock(return_value=hits)
    response = await svc.recall(
        RecallRequest(query="shared document", k=5, use_rerank=False),
        user_id=user_id,
        current_context_id=ctx.id,
        current_workspace_id=ws.id,
        **kwargs,
    )
    return response, svc.search_service.hybrid_search.await_args.kwargs["context_id"]


@pytest.mark.asyncio
@BOTH_ORDERS
async def test_point_two_rows_name_resolves_to_the_searched_context(
    db_session, point, first_row_inserted_first
):
    first_owner, first_ws = await _workspace(db_session)
    second_owner, second_ws = await _workspace(db_session)
    first_context = await _context(db_session, first_owner, first_ws)
    second_context = await _context(db_session, second_owner, second_ws)
    first_row, second_row = await _rows_in_order(
        db_session,
        first_context,
        second_context,
        point,
        first_row_inserted_first=first_row_inserted_first,
    )

    in_second, searched = await _recall(
        db_session,
        [_point_hit(second_context, point)],
        user_id=second_owner,
        ws=second_ws,
        ctx=second_context,
    )
    assert searched == str(second_context.id)
    assert [r.memory_id for r in in_second.results] == [second_row.id]
    assert in_second.results[0].summary == "row of the second context"

    in_first, _ = await _recall(
        db_session,
        [_point_hit(first_context, point)],
        user_id=first_owner,
        ws=first_ws,
        ctx=first_context,
    )
    assert [r.memory_id for r in in_first.results] == [first_row.id]


@pytest.mark.asyncio
@BOTH_ORDERS
async def test_hit_with_no_row_in_the_searched_context_returns_nothing(
    db_session, point, first_row_inserted_first
):
    """The searched context's row is forgotten and the point stays for the other row."""
    first_owner, first_ws = await _workspace(db_session)
    second_owner, second_ws = await _workspace(db_session)
    first_context = await _context(db_session, first_owner, first_ws)
    second_context = await _context(db_session, second_owner, second_ws)
    _, second_row = await _rows_in_order(
        db_session,
        first_context,
        second_context,
        point,
        first_row_inserted_first=first_row_inserted_first,
    )
    await db_session.delete(second_row)
    await db_session.commit()

    response, _ = await _recall(
        db_session,
        [_point_hit(second_context, point)],
        user_id=second_owner,
        ws=second_ws,
        ctx=second_context,
    )

    assert response.results == []


@pytest.mark.asyncio
@BOTH_ORDERS
async def test_multi_context_recall_keeps_to_the_listed_contexts(
    db_session, point, first_row_inserted_first
):
    owner, ws = await _workspace(db_session)
    first_context = await _context(db_session, owner, ws)
    second_context = await _context(db_session, owner, ws)
    third_context = await _context(db_session, owner, ws)
    _, second_row = await _rows_in_order(
        db_session,
        first_context,
        second_context,
        point,
        first_row_inserted_first=first_row_inserted_first,
    )
    third_point = uuid4()
    third_row = await _row_of_point(
        db_session, third_context, third_point, "row of the third context"
    )
    await db_session.commit()

    response, searched = await _recall(
        db_session,
        [_point_hit(second_context, point), _point_hit(third_context, third_point, score=0.8)],
        user_id=owner,
        ws=ws,
        ctx=second_context,
        context_ids=[second_context.id, third_context.id],
    )

    assert searched == [str(second_context.id), str(third_context.id)]
    assert [r.memory_id for r in response.results] == [second_row.id, third_row.id]


@pytest.mark.asyncio
@BOTH_ORDERS
@pytest.mark.parametrize("point_carries_first_context", [True, False])
async def test_multi_context_recall_takes_the_row_of_the_context_the_point_carries(
    db_session, point, first_row_inserted_first, point_carries_first_context
):
    """Both rows are in a searched context: the hit says which one it found."""
    owner, ws = await _workspace(db_session)
    first_context = await _context(db_session, owner, ws)
    second_context = await _context(db_session, owner, ws)
    first_row, second_row = await _rows_in_order(
        db_session,
        first_context,
        second_context,
        point,
        first_row_inserted_first=first_row_inserted_first,
    )
    carried, expected = (
        (first_context, first_row) if point_carries_first_context else (second_context, second_row)
    )

    response, _ = await _recall(
        db_session,
        [_point_hit(carried, point)],
        user_id=owner,
        ws=ws,
        ctx=first_context,
        context_ids=[first_context.id, second_context.id],
    )

    assert [r.memory_id for r in response.results] == [expected.id]


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


@pytest.mark.asyncio
async def test_remember_hit_still_hydrates(db_session, quiet_write):
    """A ``remember`` row's point is its own id, single-context and multi-context."""
    owner, ws = await _workspace(db_session)
    first_context = await _context(db_session, owner, ws)
    second_context = await _context(db_session, owner, ws)
    await db_session.commit()
    remembered = await MemoryService(db_session).remember(
        RememberRequest(summary="stored fact", content="body", type="fact", importance=0.6),
        user_id=owner,
        client="pytest",
        current_context_id=first_context.id,
        current_workspace_id=ws.id,
    )
    hit = _point_hit(first_context, remembered.memory_id)

    single, _ = await _recall(db_session, [hit], user_id=owner, ws=ws, ctx=first_context)
    assert [r.memory_id for r in single.results] == [remembered.memory_id]

    # A hit without a payload (a double of the search) hydrates the same way.
    bare, _ = await _recall(
        db_session,
        [{"id": hit["id"], "score": hit["score"]}],
        user_id=owner,
        ws=ws,
        ctx=first_context,
    )
    assert [r.memory_id for r in bare.results] == [remembered.memory_id]

    multi, _ = await _recall(
        db_session,
        [hit],
        user_id=owner,
        ws=ws,
        ctx=first_context,
        context_ids=[first_context.id, second_context.id],
    )
    assert [r.memory_id for r in multi.results] == [remembered.memory_id]
