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
    started_at: datetime | None = None,
) -> MemoryAnalysis:
    """Insert a MemoryAnalysis row with reasonable defaults.

    ``started_at`` overrides the offset-derived timestamp so tests can
    pin several rows to an identical instant (keyset-tiebreaker cases).
    """
    if started_at is None:
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
        content="test memory content",
        type="note",
        importance=0.5,
        tags=["t1"],
        client="test",
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
async def test_list_analyses_identical_started_at_no_skip(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    """#1247: runs sharing an identical ``started_at`` must not be skipped
    across a page boundary.

    With a strict ``started_at``-only cursor, page 2's ``started_at <
    cursor`` predicate excludes every row that shares the boundary
    timestamp, silently dropping runs. The compound ``(started_at, id)``
    keyset keeps them all reachable.
    """
    shared_ts = utcnow().replace(microsecond=0) - timedelta(minutes=5)
    runs = []
    for _ in range(4):
        run = await _make_run(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
            started_at=shared_ts,
        )
        runs.append(run)
    all_ids = {r.id for r in runs}

    # Page 1 (limit=2): 2 of the 4 identical-timestamp runs.
    page1, cursor = await query_service.list_analyses(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        limit=2,
    )
    assert len(page1) == 2
    assert cursor is not None
    # Compound cursor carries both the timestamp and the id tiebreaker.
    assert "|" in cursor

    # Page 2: the REMAINING 2 runs — none skipped despite equal started_at.
    page2, cursor2 = await query_service.list_analyses(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        limit=2,
        cursor=cursor,
    )
    assert len(page2) == 2
    assert cursor2 is None  # last page

    page1_ids = {r.id for r in page1}
    page2_ids = {r.id for r in page2}
    assert page1_ids.isdisjoint(page2_ids)
    # All four runs appear exactly once across the two pages (no skip).
    assert page1_ids | page2_ids == all_ids


@pytest.mark.asyncio
async def test_list_analyses_legacy_started_at_only_cursor_still_pages(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    """Back-compat: a pre-#1247 cursor (bare ISO ``started_at``, no id)
    still advances the page instead of erroring."""
    from utils.datetime import to_utc_iso

    for offset in (60, 30, 10):
        await _make_run(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
            started_offset_minutes=offset,
        )

    page1, _ = await query_service.list_analyses(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        limit=2,
    )
    # Simulate an in-flight legacy cursor: the started_at of the last row,
    # WITHOUT the ``|<id>`` tiebreaker suffix.
    legacy_cursor = to_utc_iso(page1[-1].started_at)
    assert "|" not in legacy_cursor

    page2, _ = await query_service.list_analyses(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        limit=2,
        cursor=legacy_cursor,
    )
    # The oldest run (60 min ago) is strictly older than page1's boundary
    # timestamp, so the legacy cursor still returns it.
    assert len(page2) == 1
    assert page2[0].started_at < page1[-1].started_at


def test_decode_list_cursor_malformed_uuid_tail_is_invalid():
    """#1247 (Copilot): a compound cursor whose ``|`` separator is present
    but whose UUID tail is malformed is a corrupt/tampered token — it must
    decode to ``(None, None)`` (invalid), NOT silently downgrade to the
    started_at-only predicate, which would reintroduce boundary-skipped
    rows for tied ``started_at`` values."""
    from services.analysis.query_service import _decode_list_cursor

    # Valid datetime + garbage UUID suffix → whole cursor invalid.
    assert _decode_list_cursor("2026-07-15T01:00:00|not-a-uuid") == (None, None)
    # Trailing separator with empty UUID tail is likewise malformed.
    assert _decode_list_cursor("2026-07-15T01:00:00|") == (None, None)

    # Contrast: a legacy bare-ISO cursor (no separator) stays a valid
    # started_at-only cursor (id=None) — back-compat preserved.
    dt, cid = _decode_list_cursor("2026-07-15T01:00:00")
    assert dt is not None and cid is None
    # And a well-formed compound cursor decodes both components.
    good_id = uuid4()
    dt, cid = _decode_list_cursor(f"2026-07-15T01:00:00|{good_id}")
    assert dt is not None and cid == good_id


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

    ids = await query_service.get_memory_ids_in_cluster(
        db_session, workspace_id=fixture_workspace_id, run_id=run.id, cluster_index=3
    )
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
    ids = await query_service.get_memory_ids_in_cluster(
        db_session, workspace_id=fixture_workspace_id, run_id=run.id, cluster_index=99
    )
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


# ===========================================================================
# #1243 — deleted-context runs are structurally invisible
# ===========================================================================


async def _soft_delete_context(db_session, context_id: UUID) -> None:
    from models.auth import Context

    ctx = await db_session.get(Context, context_id)
    ctx.deleted_at = utcnow()
    await db_session.flush()


class TestDeletedContextInvisibility:
    """#1243: every run_id-keyed reader must treat runs of a soft-deleted
    context as nonexistent. The MCP tools (get_analysis / get_cluster)
    reach these readers with only a run_id in hand — REST 404s at its
    URL-context boundary check, but MCP had no equivalent, so LLM-derived
    labels/descriptions/property_stats of deleted contexts stayed
    readable indefinitely.
    """

    @pytest.mark.asyncio
    async def test_get_analysis_none_after_context_soft_delete(
        self, db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
    ):
        run = await _make_run(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
        )
        assert (
            await query_service.get_analysis(
                db_session, workspace_id=fixture_workspace_id, run_id=run.id
            )
            is not None
        )
        await _soft_delete_context(db_session, fixture_context_id)
        assert (
            await query_service.get_analysis(
                db_session, workspace_id=fixture_workspace_id, run_id=run.id
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_get_cluster_none_after_context_soft_delete(
        self, db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
    ):
        run = await _make_run(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
        )
        await _make_cluster(db_session, run=run, cluster_index=0)
        assert (
            await query_service.get_cluster(
                db_session,
                workspace_id=fixture_workspace_id,
                run_id=run.id,
                cluster_index=0,
            )
            is not None
        )
        await _soft_delete_context(db_session, fixture_context_id)
        assert (
            await query_service.get_cluster(
                db_session,
                workspace_id=fixture_workspace_id,
                run_id=run.id,
                cluster_index=0,
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_list_clusters_none_after_context_soft_delete(
        self, db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
    ):
        run = await _make_run(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
        )
        await _make_cluster(db_session, run=run, cluster_index=0)
        await _soft_delete_context(db_session, fixture_context_id)
        assert (
            await query_service.list_clusters(
                db_session, workspace_id=fixture_workspace_id, run_id=run.id
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_list_positions_none_after_context_soft_delete(
        self, db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
    ):
        run = await _make_run(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
        )
        await _soft_delete_context(db_session, fixture_context_id)
        assert (
            await query_service.list_positions(
                db_session, workspace_id=fixture_workspace_id, run_id=run.id
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_get_memory_ids_in_cluster_none_after_context_soft_delete(
        self, db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
    ):
        run = await _make_run(
            db_session,
            workspace_id=fixture_workspace_id,
            context_id=fixture_context_id,
            pricing=fixture_pricing,
        )
        await _make_cluster(db_session, run=run, cluster_index=0)
        await _soft_delete_context(db_session, fixture_context_id)
        assert (
            await query_service.get_memory_ids_in_cluster(
                db_session,
                workspace_id=fixture_workspace_id,
                run_id=run.id,
                cluster_index=0,
            )
            is None
        )


# ===========================================================================
# #1245 — memory_id FK-cascade index parity pin
# ===========================================================================


class TestAssignmentMemoryIdIndex:
    def test_memory_id_leading_index_declared(self):
        """#1245: the memories FK needs a leading-column index — the PK
        (analysis_id, memory_id) cannot serve the RI trigger's
        memory_id-only DELETE lookup. Pins model-side parity with
        migration e62 (create_all-vs-alembic drift gate).
        """
        names = {idx.name for idx in MemoryAnalysisAssignment.__table__.indexes}
        assert "idx_memory_analysis_assignments_memory" in names
        idx = next(
            i
            for i in MemoryAnalysisAssignment.__table__.indexes
            if i.name == "idx_memory_analysis_assignments_memory"
        )
        assert [c.name for c in idx.columns] == ["memory_id"]


# ===========================================================================
# #1357 — binding subtraction on REST clusters/positions + cursor oracle
# ===========================================================================


async def _make_typed_memory(
    db_session, *, workspace_id: UUID, context_id: UUID, mem_type: str
) -> Memory:
    mem = await _make_memory(db_session, workspace_id=workspace_id, context_id=context_id)
    mem.type = mem_type
    await db_session.flush()
    return mem


async def _enforce_scope(db_session, *, workspace_id: UUID, context_id: UUID):
    """Insert an enforce-mode agent bound to ``context_id`` with a type
    restriction (only ``note`` readable) and activate its scope."""
    from uuid import uuid4 as _uuid4

    from auth.agent_scope import AgentScope, set_agent_scope
    from models.agent import Agent, AgentContextBinding

    agent = Agent(
        id=_uuid4(),
        workspace_id=workspace_id,
        name=f"binding-bot-{_uuid4().hex[:8]}",
        owner_user_id="test_user",
        status="active",
        enforcement_mode="enforce",
    )
    db_session.add(agent)
    await db_session.flush()
    db_session.add(
        AgentContextBinding(
            id=_uuid4(),
            agent_id=agent.id,
            context_id=context_id,
            can_read=True,
            write_policy="deny",
            is_default=False,
            allowed_memory_types=["note"],
            allowed_source_types=None,
            created_by="test_user",
        )
    )
    await db_session.flush()
    set_agent_scope(
        AgentScope(agent_id=agent.id, enforcement_mode="enforce", workspace_id=workspace_id)
    )
    return agent


async def _cluster_with_members(
    db_session,
    *,
    workspace_id: UUID,
    context_id: UUID,
    pricing,
    allowed: int,
    denied: int,
):
    """Run + one cluster with ``allowed`` note-memories and ``denied``
    decision-memories assigned; cluster.count stores the raw total."""
    run = await _make_run(
        db_session, workspace_id=workspace_id, context_id=context_id, pricing=pricing
    )
    mems = []
    for _ in range(allowed):
        mems.append(
            await _make_typed_memory(
                db_session, workspace_id=workspace_id, context_id=context_id, mem_type="note"
            )
        )
    denied_mems = []
    for _ in range(denied):
        denied_mems.append(
            await _make_typed_memory(
                db_session, workspace_id=workspace_id, context_id=context_id, mem_type="decision"
            )
        )
    cluster = await _make_cluster(
        db_session,
        run=run,
        cluster_index=0,
        label="alpha",
        rep_ids=[mems[0].id, denied_mems[0].id] if mems and denied_mems else [],
    )
    cluster.count = allowed + denied
    cluster.property_stats = {
        "types": {"note": allowed, "decision": denied},
        "tags": {"t1": allowed + denied},
    }
    for mem in mems + denied_mems:
        db_session.add(
            MemoryAnalysisAssignment(
                analysis_id=run.id,
                memory_id=mem.id,
                cluster_id=cluster.id,
                x=1.0,
                y=2.0,
            )
        )
    await db_session.flush()
    return run, cluster, mems, denied_mems


@pytest.mark.asyncio
async def test_get_cluster_next_cursor_never_names_a_denied_row(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    """#1357 AC2: walking every page, no next_cursor value is ever a
    binding-denied row's UUID (existence oracle, CWE-639)."""
    from auth.agent_scope import set_agent_scope

    run, _cluster, mems, denied_mems = await _cluster_with_members(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
        allowed=3,
        denied=4,
    )
    denied_ids = {str(m.id) for m in denied_mems}
    await _enforce_scope(
        db_session, workspace_id=fixture_workspace_id, context_id=fixture_context_id
    )
    try:
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page = await query_service.get_cluster(
                db_session,
                workspace_id=fixture_workspace_id,
                run_id=run.id,
                cluster_index=0,
                limit=2,
                cursor=cursor,
            )
            assert page is not None
            seen.extend(m["memory_id"] for m in page["memories"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
            assert cursor not in denied_ids
        assert cursor is None
        assert set(seen) == {str(m.id) for m in mems}
    finally:
        set_agent_scope(None)


@pytest.mark.asyncio
async def test_list_clusters_subtracts_denied_rows_for_enforce_agent(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    """#1357 AC1: REST /clusters matches MCP get_cluster subtraction —
    count/types recomputed over permitted rows, other facets fail-closed,
    denied representative ids dropped."""
    from auth.agent_scope import set_agent_scope

    run, cluster, mems, denied_mems = await _cluster_with_members(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
        allowed=2,
        denied=3,
    )
    await _enforce_scope(
        db_session, workspace_id=fixture_workspace_id, context_id=fixture_context_id
    )
    try:
        rows = await query_service.list_clusters(
            db_session, workspace_id=fixture_workspace_id, run_id=run.id
        )
        assert rows is not None and len(rows) == 1
        row = rows[0]
        count = row["count"] if isinstance(row, dict) else row.count
        stats = row["property_stats"] if isinstance(row, dict) else row.property_stats
        rep_ids = (
            row["representative_memory_ids"]
            if isinstance(row, dict)
            else row.representative_memory_ids
        )
        assert count == 2
        assert stats.get("types") == {"note": 2}
        assert "tags" not in stats  # fail-closed facet drop on subtraction
        assert [str(r) for r in rep_ids] == [str(mems[0].id)]

        # Parity with the MCP drill-down for the same cluster.
        detail = await query_service.get_cluster(
            db_session,
            workspace_id=fixture_workspace_id,
            run_id=run.id,
            cluster_index=0,
        )
        assert detail is not None and detail["count"] == count
    finally:
        set_agent_scope(None)


@pytest.mark.asyncio
async def test_list_positions_subtracts_denied_rows_for_enforce_agent(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    """#1357 AC1: REST /positions never returns a denied row's memory_id."""
    from auth.agent_scope import set_agent_scope

    run, _cluster, mems, denied_mems = await _cluster_with_members(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
        allowed=2,
        denied=2,
    )
    await _enforce_scope(
        db_session, workspace_id=fixture_workspace_id, context_id=fixture_context_id
    )
    try:
        positions = await query_service.list_positions(
            db_session, workspace_id=fixture_workspace_id, run_id=run.id
        )
        assert positions is not None
        got = {p["memory_id"] for p in positions}
        assert got == {str(m.id) for m in mems}
    finally:
        set_agent_scope(None)


@pytest.mark.asyncio
async def test_clusters_and_positions_unchanged_without_agent_scope(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    """Non-agent credentials (human UI sessions) keep the full view."""
    run, cluster, mems, denied_mems = await _cluster_with_members(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
        allowed=2,
        denied=2,
    )
    rows = await query_service.list_clusters(
        db_session, workspace_id=fixture_workspace_id, run_id=run.id
    )
    assert rows is not None and len(rows) == 1
    row = rows[0]
    count = row["count"] if isinstance(row, dict) else row.count
    stats = row["property_stats"] if isinstance(row, dict) else row.property_stats
    assert count == 4
    assert stats["types"] == {"note": 2, "decision": 2}
    assert "tags" in stats
    positions = await query_service.list_positions(
        db_session, workspace_id=fixture_workspace_id, run_id=run.id
    )
    assert positions is not None and len(positions) == 4


@pytest.mark.asyncio
async def test_get_cluster_representatives_respect_membership_gate(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    """#1357 review F1: a representative moved to an UNBOUND context after
    the analysis ran must not leak its summary — the post-fetch row filter
    alone keeps rows from contexts with no binding row."""
    from uuid import uuid4 as _uuid4

    from auth.agent_scope import set_agent_scope
    from models.auth import Context

    run, cluster, mems, _denied = await _cluster_with_members(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
        allowed=2,
        denied=0,
    )
    other_ctx = Context(
        id=_uuid4(),
        workspace_id=fixture_workspace_id,
        name="unbound_ctx",
        display_name="Unbound",
        created_by="test_user",
        is_private=False,
    )
    db_session.add(other_ctx)
    await db_session.flush()
    stray = await _make_typed_memory(
        db_session, workspace_id=fixture_workspace_id, context_id=other_ctx.id, mem_type="note"
    )
    cluster.representative_memory_ids = [mems[0].id, stray.id]
    await db_session.flush()

    await _enforce_scope(
        db_session, workspace_id=fixture_workspace_id, context_id=fixture_context_id
    )
    try:
        detail = await query_service.get_cluster(
            db_session,
            workspace_id=fixture_workspace_id,
            run_id=run.id,
            cluster_index=0,
        )
        assert detail is not None
        rep_ids = {r["memory_id"] for r in detail["representatives"]}
        assert str(stray.id) not in rep_ids
        assert str(mems[0].id) in rep_ids
    finally:
        set_agent_scope(None)


@pytest.mark.asyncio
async def test_list_clusters_omits_fully_denied_cluster(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    """#1357 review F2: a cluster with ZERO permitted rows must not be
    enumerated — its LLM label/description are synthesized from denied
    members, and the count-0-with-label shape is itself an oracle."""
    from auth.agent_scope import set_agent_scope

    run, _cluster, mems, _denied = await _cluster_with_members(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
        allowed=1,
        denied=0,
    )
    denied_only = await _make_typed_memory(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        mem_type="decision",
    )
    cluster2 = await _make_cluster(db_session, run=run, cluster_index=1, label="denied topic")
    cluster2.count = 1
    db_session.add(
        MemoryAnalysisAssignment(
            analysis_id=run.id, memory_id=denied_only.id, cluster_id=cluster2.id, x=0.0, y=0.0
        )
    )
    await db_session.flush()

    await _enforce_scope(
        db_session, workspace_id=fixture_workspace_id, context_id=fixture_context_id
    )
    try:
        rows = await query_service.list_clusters(
            db_session, workspace_id=fixture_workspace_id, run_id=run.id
        )
        assert rows is not None
        labels = [r["label"] if isinstance(r, dict) else r.label for r in rows]
        assert labels == ["alpha"]  # the fully-denied cluster is omitted
    finally:
        set_agent_scope(None)

    # Human view keeps both.
    rows = await query_service.list_clusters(
        db_session, workspace_id=fixture_workspace_id, run_id=run.id
    )
    assert rows is not None and len(rows) == 2


@pytest.mark.asyncio
async def test_shadow_scope_keeps_full_view(
    db_session, fixture_workspace_id, fixture_context_id, fixture_pricing
):
    """Shadow enforcement must change nothing observable on any of the
    three surfaces (predicate is None for shadow scopes)."""
    from auth.agent_scope import AgentScope, set_agent_scope

    run, cluster, mems, denied_mems = await _cluster_with_members(
        db_session,
        workspace_id=fixture_workspace_id,
        context_id=fixture_context_id,
        pricing=fixture_pricing,
        allowed=2,
        denied=2,
    )
    agent = await _enforce_scope(
        db_session, workspace_id=fixture_workspace_id, context_id=fixture_context_id
    )
    set_agent_scope(
        AgentScope(agent_id=agent.id, enforcement_mode="shadow", workspace_id=fixture_workspace_id)
    )
    try:
        rows = await query_service.list_clusters(
            db_session, workspace_id=fixture_workspace_id, run_id=run.id
        )
        assert rows is not None and len(rows) == 1
        first = rows[0]
        assert (first["count"] if isinstance(first, dict) else first.count) == 4
        positions = await query_service.list_positions(
            db_session, workspace_id=fixture_workspace_id, run_id=run.id
        )
        assert positions is not None and len(positions) == 4
        detail = await query_service.get_cluster(
            db_session,
            workspace_id=fixture_workspace_id,
            run_id=run.id,
            cluster_index=0,
        )
        assert detail is not None
        assert len(detail["memories"]) == 4
        assert detail["count"] == 4
    finally:
        set_agent_scope(None)
