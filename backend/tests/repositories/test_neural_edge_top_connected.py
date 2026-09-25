"""``NeuralEdgeRepository.get_top_connected_nodes`` (#1695).

The query aggregates a ``UNION ALL`` of out- and in-degree counts. It used to
read the union's columns through ``CompoundSelect.c``, an implicit-subquery
shortcut SQLAlchemy deprecated in 1.4 and removed in 2.1 — building the
statement raised ``AttributeError`` before it reached the database, so the
graph "top nodes" read failed on every call.
"""

from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from models.memory import EDGE_ORIGIN_SEMANTIC, NeuralMemoryEdge
from repositories.neural_edge import NeuralEdgeRepository


@pytest.mark.asyncio
async def test_statement_builds_and_aggregates_the_union(mocker) -> None:
    """No DB needed: the failure was in statement construction."""
    db = mocker.AsyncMock()
    db.execute.return_value = mocker.MagicMock(all=mocker.MagicMock(return_value=[]))
    repo = NeuralEdgeRepository(db)

    result = await repo.get_top_connected_nodes(
        workspace_id=str(uuid4()), context_id=str(uuid4()), limit=5
    )

    assert result == []
    stmt = db.execute.await_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect())).lower()
    assert "union all" in sql
    assert "sum(" in sql
    assert "group by" in sql


def _edge(src, dst, user_id: str) -> NeuralMemoryEdge:
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
async def test_total_degree_counts_both_directions(db_session, sample_memory_triple) -> None:
    """A→B, A→C, B→A: A has degree 3 (2 out + 1 in), B 2, C 1."""
    a, b, c = sample_memory_triple
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
