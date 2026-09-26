"""``NeuralEdgeRepository.get_top_connected_nodes`` against Postgres (#1695).

The query aggregates a ``UNION ALL`` of out- and in-degree counts. SQLAlchemy
2.1 removed ``CompoundSelect.c``, which the query used to read the union's
columns, so the statement is now read through an explicit subquery. The
statement-construction check lives in
``tests/repositories/test_neural_edge_top_connected.py`` (no DB); this test
pins the result the rewritten query returns, and lives under
``tests/integration/`` so the CI integration job (the one with Postgres)
runs it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from models.auth import Context, Workspace
from models.memory import EDGE_ORIGIN_SEMANTIC, Memory, NeuralMemoryEdge
from repositories.neural_edge import NeuralEdgeRepository


@pytest_asyncio.fixture
async def three_memories(db_session: AsyncSession) -> tuple[Memory, Memory, Memory]:
    """Three memories (A, B, C) in one workspace + context."""
    owner_id = f"owner_{uuid4().hex[:8]}"
    ws = Workspace(
        id=uuid4(),
        name=f"top-nodes-ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id=owner_id,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"top-nodes-ctx-{uuid4().hex[:8]}",
        created_by=owner_id,
        is_private=False,
    )
    nodes = tuple(
        Memory(
            id=uuid4(),
            user_id=owner_id,
            workspace_id=ws.id,
            context_id=ctx.id,
            summary=f"node {label}",
            content=f"{label} content",
            type="note",
            client="test",
        )
        for label in ("A", "B", "C")
    )

    # Flush in dependency order: the models carry raw FK columns without
    # relationship(), so the unit of work does not order these inserts.
    db_session.add(ws)
    await db_session.flush()
    db_session.add(ctx)
    await db_session.flush()
    db_session.add_all(nodes)
    await db_session.flush()
    a, b, c = nodes
    return a, b, c


def _edge(src: Memory, dst: Memory, user_id: str) -> NeuralMemoryEdge:
    return NeuralMemoryEdge(
        user_id=user_id,
        src_id=src.id,
        dst_id=dst.id,
        workspace_id=src.workspace_id,
        context_id=src.context_id,
        edge_type="related_to",
        weight=0.5,
        confidence=1.0,
        origin=EDGE_ORIGIN_SEMANTIC,
    )


@pytest.mark.asyncio
async def test_total_degree_counts_both_directions(
    db_session: AsyncSession, three_memories: tuple[Memory, Memory, Memory]
) -> None:
    """A→B, A→C, B→A: A has degree 3 (2 out + 1 in), B 2, C 1."""
    a, b, c = three_memories
    user_id = f"top-nodes-{uuid4().hex[:8]}"
    db_session.add_all([_edge(a, b, user_id), _edge(a, c, user_id), _edge(b, a, user_id)])
    await db_session.flush()

    repo = NeuralEdgeRepository(db_session)
    ranked = await repo.get_top_connected_nodes(
        user_id=user_id,
        workspace_id=str(a.workspace_id),
        context_id=str(a.context_id),
        limit=10,
    )

    assert ranked == [(a.id, 3), (b.id, 2), (c.id, 1)]
