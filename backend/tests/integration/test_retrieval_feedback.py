"""Integration test for the retrieval feedback signal (Issue #888).

Exercises the real DB path: FeedbackService writes append-only rows into the
dedicated ``retrieval_feedback`` table (NOT ``memories``), aggregation tallies
net-helpful, and the lane is structurally isolated from recall (separate table,
no row ever lands in ``memories``). Also pins the FK CASCADE erasure contract.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, Workspace
from models.memory import Memory
from models.retrieval_feedback import RetrievalFeedback
from services.feedback_service import FeedbackService
from utils.exceptions import NotFoundException


@pytest_asyncio.fixture
async def feedback_scenario(db_session: AsyncSession):
    owner = f"owner_{uuid4().hex[:8]}"
    ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    db_session.add(ws)
    await db_session.flush()
    db_session.add(ctx)
    await db_session.flush()
    mem = Memory(
        id=uuid4(),
        user_id=owner,
        workspace_id=ws.id,
        context_id=ctx.id,
        summary="a memory to rate",
        content="content",
        type="note",
        client="test",
        tags=[],
    )
    db_session.add(mem)
    await db_session.flush()
    return {"owner": owner, "ws": ws, "ctx": ctx, "mem": mem}


@pytest.mark.asyncio
async def test_feedback_is_append_only_and_isolated_from_memories(
    db_session: AsyncSession, feedback_scenario
):
    s = feedback_scenario
    svc = FeedbackService(db_session)
    mem_count_before = (
        await db_session.execute(
            select(func.count()).select_from(Memory).where(Memory.context_id == s["ctx"].id)
        )
    ).scalar_one()

    # Append two events for the same memory (append-only: 2 rows, no upsert).
    r1 = await svc.record_feedback(s["ctx"].id, s["mem"].id, True, s["owner"], query="q1")
    r2 = await svc.record_feedback(s["ctx"].id, s["mem"].id, False, s["owner"], query="q1")

    fb_count = (
        await db_session.execute(
            select(func.count())
            .select_from(RetrievalFeedback)
            .where(RetrievalFeedback.memory_id == s["mem"].id)
        )
    ).scalar_one()
    assert fb_count == 2, "feedback is append-only — two calls must persist two rows"

    # Recording feedback must NOT touch the memories table (no recall pollution).
    mem_count_after = (
        await db_session.execute(
            select(func.count()).select_from(Memory).where(Memory.context_id == s["ctx"].id)
        )
    ).scalar_one()
    assert mem_count_after == mem_count_before, "feedback must not insert into memories"

    # Structural isolation: a feedback row's id is NOT a memory id — the lane is a
    # separate table, so recall's candidate query (select(Memory)) can never see it.
    feedback_ids = [r1.id, r2.id]
    leaked = (
        (await db_session.execute(select(Memory.id).where(Memory.id.in_(feedback_ids))))
        .scalars()
        .all()
    )
    assert not leaked, "a feedback id must never resolve to a memory row (recall isolation)"


@pytest.mark.asyncio
async def test_aggregate_for_memory_tallies_net_helpful(
    db_session: AsyncSession, feedback_scenario
):
    s = feedback_scenario
    svc = FeedbackService(db_session)
    await svc.record_feedback(s["ctx"].id, s["mem"].id, True, s["owner"])
    await svc.record_feedback(s["ctx"].id, s["mem"].id, True, s["owner"])
    await svc.record_feedback(s["ctx"].id, s["mem"].id, False, s["owner"])

    agg = await svc.aggregate_for_memory(s["ctx"].id, s["mem"].id)
    assert agg.helpful_count == 2
    assert agg.not_helpful_count == 1
    assert agg.net == 1


@pytest.mark.asyncio
async def test_aggregate_isolates_by_memory(db_session: AsyncSession, feedback_scenario):
    """Aggregation must not bleed in feedback from a different memory in the context."""
    s = feedback_scenario
    svc = FeedbackService(db_session)
    other = Memory(
        id=uuid4(),
        user_id=s["owner"],
        workspace_id=s["ws"].id,
        context_id=s["ctx"].id,
        summary="a sibling memory",
        content="content",
        type="note",
        client="test",
        tags=[],
    )
    db_session.add(other)
    await db_session.flush()

    await svc.record_feedback(s["ctx"].id, s["mem"].id, True, s["owner"])
    # 3 not-helpful events for the SIBLING memory — must NOT count toward mem.
    for _ in range(3):
        await svc.record_feedback(s["ctx"].id, other.id, False, s["owner"])

    agg = await svc.aggregate_for_memory(s["ctx"].id, s["mem"].id)
    assert agg.helpful_count == 1
    assert agg.not_helpful_count == 0, "aggregation leaked feedback from a different memory"


@pytest.mark.asyncio
async def test_query_is_truncated_to_column_limit(db_session: AsyncSession, feedback_scenario):
    s = feedback_scenario
    svc = FeedbackService(db_session)
    row = await svc.record_feedback(s["ctx"].id, s["mem"].id, True, s["owner"], query="x" * 5000)
    assert row.query is not None
    assert len(row.query) == 1024, "query must be truncated to the column limit"


@pytest.mark.asyncio
async def test_note_is_truncated(db_session: AsyncSession, feedback_scenario):
    s = feedback_scenario
    svc = FeedbackService(db_session)
    row = await svc.record_feedback(s["ctx"].id, s["mem"].id, False, s["owner"], note="y" * 5000)
    assert row.note is not None
    assert len(row.note) == 2000, "note must be truncated to bound retention"


@pytest.mark.asyncio
async def test_feedback_for_memory_outside_context_is_rejected(
    db_session: AsyncSession, feedback_scenario
):
    """A memory_id that does not belong to the context is rejected (cross-context
    signal-injection guard) — the access gate is on the context only."""
    s = feedback_scenario
    # A second context (same workspace) with its own memory.
    other_ctx = Context(
        id=uuid4(),
        workspace_id=s["ws"].id,
        name=f"other-{uuid4().hex[:8]}",
        created_by=s["owner"],
        is_private=False,
    )
    db_session.add(other_ctx)
    await db_session.flush()
    foreign_mem = Memory(
        id=uuid4(),
        user_id=s["owner"],
        workspace_id=s["ws"].id,
        context_id=other_ctx.id,
        summary="memory in another context",
        content="content",
        type="note",
        client="test",
        tags=[],
    )
    db_session.add(foreign_mem)
    await db_session.flush()

    svc = FeedbackService(db_session)
    # Rate the foreign memory while "authorized" only on s['ctx'] → must reject.
    with pytest.raises(NotFoundException):
        await svc.record_feedback(s["ctx"].id, foreign_mem.id, True, s["owner"])


@pytest.mark.asyncio
async def test_memory_delete_cascades_feedback(db_session: AsyncSession, feedback_scenario):
    """Deleting the rated memory cascades its feedback (isolates the memory_id FK)."""
    s = feedback_scenario
    svc = FeedbackService(db_session)
    await svc.record_feedback(s["ctx"].id, s["mem"].id, True, s["owner"])

    # Delete ONLY the memory (context stays) so this exercises the memory_id
    # CASCADE specifically — a missing ondelete='CASCADE' there fails here.
    await db_session.execute(delete(Memory).where(Memory.id == s["mem"].id))
    await db_session.commit()

    remaining = (
        await db_session.execute(
            select(func.count())
            .select_from(RetrievalFeedback)
            .where(RetrievalFeedback.memory_id == s["mem"].id)
        )
    ).scalar_one()
    assert remaining == 0, "feedback must cascade-delete with its memory"


@pytest.mark.asyncio
async def test_context_delete_cascades_feedback(db_session: AsyncSession, feedback_scenario):
    s = feedback_scenario
    svc = FeedbackService(db_session)
    await svc.record_feedback(s["ctx"].id, s["mem"].id, True, s["owner"])

    # Deleting the context must cascade-delete its feedback (erasure contract).
    await db_session.execute(delete(Memory).where(Memory.context_id == s["ctx"].id))
    await db_session.execute(delete(Context).where(Context.id == s["ctx"].id))
    await db_session.commit()

    remaining = (
        await db_session.execute(
            select(func.count())
            .select_from(RetrievalFeedback)
            .where(RetrievalFeedback.context_id == s["ctx"].id)
        )
    ).scalar_one()
    assert remaining == 0, "feedback must cascade-delete with its context"
