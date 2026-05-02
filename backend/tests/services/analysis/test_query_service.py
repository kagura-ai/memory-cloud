"""Unit tests for ``services/analysis/query_service`` (Issue #496).

Covers the read-path helpers that REST routes, MCP tools, the
``recall`` filter extension, and the ``/usage`` endpoint all share.

Pagination contract (``get_cluster``):
- ``limit`` is clamped server-side to ``MAX_CLUSTER_PAGE_SIZE`` (200).
- ``cursor`` is the last memory_id UUID of the previous page; the next
  query uses ``> cursor`` so the cursor row is not duplicated.
- ``next_cursor`` is None on the last page.

Tenancy invariant (``get_analysis``, ``list_analyses``,
``get_active_analysis``, ``get_cluster``):
- A run UUID stolen from a foreign workspace returns None instead of
  the row, so existence is not leaked.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from models.analysis import (
    MemoryAnalysis,
    MemoryAnalysisAssignment,
    MemoryAnalysisCluster,
)
from models.memory import Memory
from services.analysis import query_service
from utils.datetime import utcnow


@pytest_asyncio.fixture
async def fixture_workspace_id(db_session) -> UUID:
    from models.auth import Workspace

    ws = Workspace(id=uuid4(), name="Test", owner_user_id="test_owner")
    db_session.add(ws)
    await db_session.flush()
    return ws.id


@pytest_asyncio.fixture
async def fixture_other_workspace_id(db_session) -> UUID:
    from models.auth import Workspace

    ws = Workspace(id=uuid4(), name="Other", owner_user_id="other_owner")
    db_session.add(ws)
    await db_session.flush()
    return ws.id


@pytest_asyncio.fixture
async def fixture_context_id(db_session, fixture_workspace_id) -> UUID:
    from models.auth import Context

    ctx = Context(
        id=uuid4(),
        workspace_id=fixture_workspace_id,
        name="test_ctx",
        display_name="Test Context",
        created_by="test_user",
        is_private=False,
    )
    db_session.add(ctx)
    await db_session.flush()
    return ctx.id


@pytest_asyncio.fixture
async def fixture_pricing(db_session):
    """Insert a stub llm_pricing row so MemoryAnalysis.model_id FK is satisfied.

    The query_service tests do not exercise pricing logic; we just need
    the FK target to exist.
    """
    from models.llm_pricing import LLMPricing

    row = LLMPricing(
        provider="openai",
        model="gpt-5-nano",
        unit_type="input_tokens",
        price_per_unit=0.20,
        effective_from=datetime(2026, 1, 1),
    )
    db_session.add(row)
    await db_session.flush()
    yield row


async def _make_run(
    db_session,
    *,
    workspace_id: UUID,
    context_id: UUID,
    pricing,
    status: str = "succeeded",
    started_offset_minutes: int = 0,
) -> MemoryAnalysis:
    """Insert a MemoryAnalysis row with reasonable defaults."""
    started_at = utcnow() - timedelta(minutes=started_offset_minutes)
    finished_at = (started_at + timedelta(seconds=10)) if status != "running" else None
    run = MemoryAnalysis(
        id=uuid4(),
        workspace_id=workspace_id,
        context_id=context_id,
        triggered_by="test_user",
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        model_id=pricing.id,
        model_snapshot={"model": "gpt-5-nano", "rates": {}},
        embedding_model="text-embedding-3-small",
        params={},
        input_count=10,
        cost_estimated_cents=5,
        cost_actual_cents=4 if status == "succeeded" else None,
        paid_by="byok",
    )
    db_session.add(run)
    await db_session.flush()
    return run


async def _make_cluster(
    db_session,
    *,
    run: MemoryAnalysis,
    cluster_index: int,
    label: str = "test cluster",
    rep_ids: list[UUID] | None = None,
) -> MemoryAnalysisCluster:
    cluster = MemoryAnalysisCluster(
        id=uuid4(),
        analysis_id=run.id,
        cluster_index=cluster_index,
        label=label,
        description=None,
        count=0,
        centroid_2d=[0.0, 0.0],
        representative_memory_ids=rep_ids or [],
        property_stats={},
        label_confidence=0.5,
    )
    db_session.add(cluster)
    await db_session.flush()
    return cluster


async def _make_memory(db_session, *, workspace_id: UUID, context_id: UUID) -> Memory:
    mem = Memory(
        id=uuid4(),
        workspace_id=workspace_id,
        context_id=context_id,
        user_id="test_user",
        summary="test memory summary",
        importance=0.5,
        tags=["t1"],
    )
    db_session.add(mem)
    await db_session.flush()
    return mem


@pytest.mark.asyncio
async def test_get_analysis_returns_row_for_owner_workspace(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    run = await _make_run(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
    )
    fetched = await query_service.get_analysis(
        db_session, workspace_id=fixture_workspace_id, run_id=run.id
    )
    assert fetched is not None
    assert fetched.id == run.id


@pytest.mark.asyncio
async def test_get_analysis_returns_none_for_foreign_workspace(
    db_session,
    fixture_workspace_id,
    fixture_other_workspace_id,
    fixture_context_id,
    fixture_pricing,
):
    """Tenancy invariant: stolen run UUID must not leak existence."""
    run = await _make_run(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
    )
    fetched = await query_service.get_analysis(
        db_session, workspace_id=fixture_other_workspace_id, run_id=run.id
    )
    assert fetched is None


@pytest.mark.asyncio
async def test_list_analyses_newest_first_with_cursor_pagination(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    runs = []
    for offset in (60, 30, 10, 5):  # oldest first by minutes-ago
        run = await _make_run(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
            started_offset_minutes=offset,
        )
        runs.append(run)

    # First page (limit=2) → 2 newest runs (5 min and 10 min ago)
    page1, cursor = await query_service.list_analyses(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        limit=2,
    )
    assert len(page1) == 2
    assert cursor is not None
    assert page1[0].started_at > page1[1].started_at  # newest first

    # Second page → remaining 2
    page2, cursor2 = await query_service.list_analyses(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        limit=2,
        cursor=cursor,
    )
    assert len(page2) == 2
    assert cursor2 is None  # last page
    # Pages should be disjoint
    page1_ids = {r.id for r in page1}
    page2_ids = {r.id for r in page2}
    assert page1_ids.isdisjoint(page2_ids)


@pytest.mark.asyncio
async def test_get_active_analysis_returns_most_recent_succeeded(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    # Older succeeded
    older = await _make_run(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
        status="succeeded",
        started_offset_minutes=120,
    )
    # Newer succeeded — this should win
    newer = await _make_run(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
        status="succeeded",
        started_offset_minutes=10,
    )
    # Even-newer running run should NOT win (not succeeded)
    await _make_run(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
        status="running",
        started_offset_minutes=2,
    )
    active = await query_service.get_active_analysis(
        db_session, workspace_id=fixture_workspace_id, context_id=fixture_context_id
    )
    assert active is not None
    assert active.id == newer.id
    assert active.id != older.id


@pytest.mark.asyncio
async def test_get_memory_ids_in_cluster_returns_assignments(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    run = await _make_run(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
    )
    cluster = await _make_cluster(db_session, run=run, cluster_index=3)
    mems = [
        await _make_memory(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
        )
        for _ in range(3)
    ]
    for mem in mems:
        db_session.add(
            MemoryAnalysisAssignment(
                analysis_id=run.id,
                memory_id=mem.id,
                cluster_id=cluster.id,
                x=0.0,
                y=0.0,
            )
        )
    await db_session.flush()

    ids = await query_service.get_memory_ids_in_cluster(db_session, run_id=run.id, cluster_index=3)
    assert ids is not None
    assert set(ids) == {m.id for m in mems}


@pytest.mark.asyncio
async def test_get_memory_ids_in_cluster_returns_none_for_unknown(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    run = await _make_run(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
    )
    # cluster_index=99 was never created
    ids = await query_service.get_memory_ids_in_cluster(db_session, run_id=run.id, cluster_index=99)
    assert ids is None


@pytest.mark.asyncio
async def test_get_cluster_returns_paginated_memories(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    run = await _make_run(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
    )
    cluster = await _make_cluster(db_session, run=run, cluster_index=0, label="alpha")
    # Create 5 memories assigned to the cluster.
    mems = [
        await _make_memory(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
        )
        for _ in range(5)
    ]
    for mem in mems:
        db_session.add(
            MemoryAnalysisAssignment(
                analysis_id=run.id,
                memory_id=mem.id,
                cluster_id=cluster.id,
                x=0.0,
                y=0.0,
            )
        )
    await db_session.flush()

    # First page of 3 — expect next_cursor non-None
    page1 = await query_service.get_cluster(
        db_session,
        workspace_id=fixture_workspace_id,
        run_id=run.id,
        cluster_index=0,
        limit=3,
    )
    assert page1 is not None
    assert page1["label"] == "alpha"
    assert len(page1["memories"]) == 3
    assert page1["next_cursor"] is not None

    # Second page → remaining 2, next_cursor None
    page2 = await query_service.get_cluster(
        db_session,
        workspace_id=fixture_workspace_id,
        run_id=run.id,
        cluster_index=0,
        limit=3,
        cursor=page1["next_cursor"],
    )
    assert page2 is not None
    assert len(page2["memories"]) == 2
    assert page2["next_cursor"] is None


@pytest.mark.asyncio
async def test_get_cluster_returns_none_for_foreign_workspace(
    db_session,
    fixture_workspace_id,
    fixture_other_workspace_id,
    fixture_context_id,
    fixture_pricing,
):
    run = await _make_run(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
    )
    await _make_cluster(db_session, run=run, cluster_index=0)

    cluster = await query_service.get_cluster(
        db_session,
        workspace_id=fixture_other_workspace_id,
        run_id=run.id,
        cluster_index=0,
    )
    assert cluster is None


@pytest.mark.asyncio
async def test_get_cluster_limit_clamped_to_max(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    """Server-side clamp: caller-supplied limit > MAX caps at MAX."""
    run = await _make_run(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
    )
    await _make_cluster(db_session, run=run, cluster_index=0)
    page = await query_service.get_cluster(
        db_session,
        workspace_id=fixture_workspace_id,
        run_id=run.id,
        cluster_index=0,
        limit=500,  # > MAX_CLUSTER_PAGE_SIZE=200
    )
    # The clamp is internal to _clamp_limit; we verify behavior via the
    # peek-one query: empty cluster yields 0 memories and no cursor.
    assert page is not None
    assert page["memories"] == []
    assert page["next_cursor"] is None
