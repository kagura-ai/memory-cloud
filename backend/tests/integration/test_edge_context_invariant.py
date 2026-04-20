"""Integration test for the edge context invariant (#396 AC 6).

Pins the behavior that ``NeuralEdgeRepository.create_or_update_edge`` and
``create_edge_if_absent`` refuse to persist an edge when either endpoint
memory does not live in the same ``(workspace_id, context_id)`` the edge
is being written into. The check is implemented at the repository layer
so every caller — ``GraphService.add_edge`` (Hebbian hot path), sleep
edge discovery, and the MCP ``create_edge`` / ``update_edge`` tools —
inherits the guarantee without having to duplicate the assertion.

Two positive cases are included so a future regression that over-rejects
legitimate same-context writes also trips the suite.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, Workspace
from models.memory import Memory
from repositories.neural_edge import NeuralEdgeRepository


@pytest_asyncio.fixture
async def invariant_scenario(db_session: AsyncSession):
    """Two contexts, three memories: src+dst in ctx_A, plus one stray in ctx_B."""
    owner_id = f"owner_{uuid4().hex[:8]}"

    ws = Workspace(
        id=uuid4(),
        name=f"ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner_id,
        memory_limit=100000,
        daily_api_limit=50000,
        weekly_api_limit=250000,
    )
    ctx_a = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-a-{uuid4().hex[:8]}",
        created_by=owner_id,
        is_private=False,
    )
    ctx_b = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-b-{uuid4().hex[:8]}",
        created_by=owner_id,
        is_private=False,
    )

    def _mk_memory(ctx: Context) -> Memory:
        return Memory(
            id=uuid4(),
            user_id=owner_id,
            workspace_id=ws.id,
            context_id=ctx.id,
            summary=f"mem-{uuid4().hex[:6]}",
            content="x",
            type="note",
            client="test",
        )

    mem_src_a = _mk_memory(ctx_a)
    mem_dst_a = _mk_memory(ctx_a)
    mem_stray_b = _mk_memory(ctx_b)

    # Flush in dependency order — SQLAlchemy's UoW heuristic doesn't reliably
    # topo-sort inserts when the ORM lacks back-populating relationship() on
    # Context → Workspace and Memory → Context (they only have raw FK columns),
    # so a single add_all + flush can see contexts inserted before their
    # workspace row exists. test_graph_visibility.py has the same pattern.
    db_session.add(ws)
    await db_session.flush()

    db_session.add_all([ctx_a, ctx_b])
    await db_session.flush()

    db_session.add_all([mem_src_a, mem_dst_a, mem_stray_b])
    await db_session.flush()

    yield {
        "owner_id": owner_id,
        "ws_id": ws.id,
        "ctx_a_id": ctx_a.id,
        "ctx_b_id": ctx_b.id,
        "mem_src_a": mem_src_a,
        "mem_dst_a": mem_dst_a,
        "mem_stray_b": mem_stray_b,
    }

    await db_session.rollback()


@pytest.mark.asyncio
async def test_create_or_update_edge_rejects_cross_context_dst(
    db_session: AsyncSession, invariant_scenario
):
    """Edge with src in ctx_A and dst in ctx_B must raise ValueError."""
    repo = NeuralEdgeRepository(db_session)
    s = invariant_scenario

    with pytest.raises(ValueError, match="edge context invariant violated"):
        await repo.create_or_update_edge(
            user_id=s["owner_id"],
            src_id=s["mem_src_a"].id,
            dst_id=s["mem_stray_b"].id,
            workspace_id=str(s["ws_id"]),
            context_id=str(s["ctx_a_id"]),
        )


@pytest.mark.asyncio
async def test_create_or_update_edge_rejects_cross_context_src(
    db_session: AsyncSession, invariant_scenario
):
    """Edge with src in ctx_B and dst in ctx_A must raise — symmetric to the dst case."""
    repo = NeuralEdgeRepository(db_session)
    s = invariant_scenario

    with pytest.raises(ValueError, match="edge context invariant violated"):
        await repo.create_or_update_edge(
            user_id=s["owner_id"],
            src_id=s["mem_stray_b"].id,
            dst_id=s["mem_dst_a"].id,
            workspace_id=str(s["ws_id"]),
            context_id=str(s["ctx_a_id"]),
        )


@pytest.mark.asyncio
async def test_create_or_update_edge_rejects_missing_endpoint(
    db_session: AsyncSession, invariant_scenario
):
    """Edge pointing at a memory id that doesn't exist must raise (fails closed)."""
    repo = NeuralEdgeRepository(db_session)
    s = invariant_scenario
    bogus_id = uuid4()

    with pytest.raises(ValueError, match="memory not found"):
        await repo.create_or_update_edge(
            user_id=s["owner_id"],
            src_id=s["mem_src_a"].id,
            dst_id=bogus_id,
            workspace_id=str(s["ws_id"]),
            context_id=str(s["ctx_a_id"]),
        )


@pytest.mark.asyncio
async def test_create_or_update_edge_accepts_same_context(
    db_session: AsyncSession, invariant_scenario
):
    """Same-context edge is the happy path — invariant check must not over-reject."""
    repo = NeuralEdgeRepository(db_session)
    s = invariant_scenario

    edge = await repo.create_or_update_edge(
        user_id=s["owner_id"],
        src_id=s["mem_src_a"].id,
        dst_id=s["mem_dst_a"].id,
        workspace_id=str(s["ws_id"]),
        context_id=str(s["ctx_a_id"]),
    )
    assert edge.src_id == s["mem_src_a"].id
    assert edge.dst_id == s["mem_dst_a"].id
    assert edge.workspace_id == s["ws_id"]
    assert edge.context_id == s["ctx_a_id"]


@pytest.mark.asyncio
async def test_create_or_update_edge_rejects_soft_deleted_endpoint(
    db_session: AsyncSession, invariant_scenario
):
    """Soft-deleted endpoint memory must raise — fails closed even though the row exists.

    Copilot loop 1 catch: the original SELECT filtered only on ``Memory.id``,
    so a soft-deleted endpoint would be treated as valid. Writing new edges
    to soft-deleted memories would defeat the application-layer soft-delete
    semantics (undo, GDPR replay) and could resurrect state that the deleter
    intended to retire.
    """
    repo = NeuralEdgeRepository(db_session)
    s = invariant_scenario

    # Soft-delete dst. Memory.deleted_at is TIMESTAMP WITHOUT TIME ZONE in
    # this schema (see models/memory.py), so use a tz-naive datetime.
    s["mem_dst_a"].deleted_at = datetime.utcnow()
    s["mem_dst_a"].deleted_by = s["owner_id"]
    await db_session.flush()

    with pytest.raises(ValueError, match="memory not found"):
        await repo.create_or_update_edge(
            user_id=s["owner_id"],
            src_id=s["mem_src_a"].id,
            dst_id=s["mem_dst_a"].id,
            workspace_id=str(s["ws_id"]),
            context_id=str(s["ctx_a_id"]),
        )


@pytest.mark.asyncio
async def test_create_edge_if_absent_rejects_cross_context(
    db_session: AsyncSession, invariant_scenario
):
    """create_edge_if_absent (k-NN seeding path) also honors the invariant."""
    repo = NeuralEdgeRepository(db_session)
    s = invariant_scenario

    with pytest.raises(ValueError, match="edge context invariant violated"):
        await repo.create_edge_if_absent(
            user_id=s["owner_id"],
            src_id=s["mem_src_a"].id,
            dst_id=s["mem_stray_b"].id,
            edge_type="semantic_similarity",
            weight=0.5,
            workspace_id=str(s["ws_id"]),
            context_id=str(s["ctx_a_id"]),
        )
