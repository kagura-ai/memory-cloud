"""Origin-aware conflict resolution in transfer_edges (Issue #725).

Sleep dedup merge calls NeuralEdgeRepository.transfer_edges to reassign edges
from a merged (loser) memory onto the winner. When both memories have an edge
to the same neighbor, the resulting (winner, neighbor) edges conflict. Before
#725 the tie-break was weight-only, so a heavier hebbian edge could silently
delete a lighter semantic edge. These tests pin the origin-aware behavior:
a non-hebbian (semantic/declared) edge must survive a hebbian conflict
regardless of weight, while same-origin-tier conflicts still use weight.
"""

import pytest

from models.memory import (
    EDGE_ORIGIN_HEBBIAN,
    EDGE_ORIGIN_SEMANTIC,
    NeuralMemoryEdge,
)
from repositories.neural_edge import NeuralEdgeRepository


def _make_edge(src, neighbor, *, origin, weight):
    """Build a src->neighbor edge sharing the nodes' workspace/context."""
    return NeuralMemoryEdge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=neighbor.id,
        workspace_id=src.workspace_id,
        context_id=src.context_id,
        edge_type="related_to" if origin == EDGE_ORIGIN_SEMANTIC else "neural_association",
        weight=weight,
        confidence=1.0,
        origin=origin,
    )


async def _merge_b_into_a(db_session, a, b):
    """Run transfer_edges(from=B, to=A) and flush pending mutations."""
    repo = NeuralEdgeRepository(db_session)
    transferred = await repo.transfer_edges(
        from_node_id=b.id,
        to_node_id=a.id,
        user_id=a.user_id,
        workspace_id=str(a.workspace_id),
        context_id=str(a.context_id),
    )
    await db_session.flush()
    return repo, transferred


@pytest.mark.asyncio
async def test_transfer_semantic_survives_heavier_hebbian(db_session, sample_memory_triple):
    """A(semantic 0.75 -> C) + B(hebbian 0.80 -> C); merging B into A keeps semantic."""
    a, b, c = sample_memory_triple
    db_session.add_all(
        [
            _make_edge(a, c, origin=EDGE_ORIGIN_SEMANTIC, weight=0.75),
            _make_edge(b, c, origin=EDGE_ORIGIN_HEBBIAN, weight=0.80),
        ]
    )
    await db_session.flush()

    repo, _ = await _merge_b_into_a(db_session, a, b)

    surviving = await repo.get_edge(a.user_id, a.id, c.id)
    assert surviving is not None
    assert surviving.origin == EDGE_ORIGIN_SEMANTIC  # not displaced by heavier hebbian
    assert surviving.weight == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_transfer_same_origin_uses_weight_tiebreak(db_session, sample_memory_triple):
    """A(semantic 0.75 -> C) + B(semantic 0.80 -> C); same tier -> heavier wins."""
    a, b, c = sample_memory_triple
    db_session.add_all(
        [
            _make_edge(a, c, origin=EDGE_ORIGIN_SEMANTIC, weight=0.75),
            _make_edge(b, c, origin=EDGE_ORIGIN_SEMANTIC, weight=0.80),
        ]
    )
    await db_session.flush()

    repo, _ = await _merge_b_into_a(db_session, a, b)

    surviving = await repo.get_edge(a.user_id, a.id, c.id)
    assert surviving is not None
    assert surviving.origin == EDGE_ORIGIN_SEMANTIC
    assert surviving.weight == pytest.approx(0.80)  # heavier incoming replaced existing


@pytest.mark.asyncio
async def test_transfer_semantic_replaces_heavier_hebbian_existing(
    db_session, sample_memory_triple
):
    """A(hebbian 0.80 -> C) + B(semantic 0.75 -> C); incoming semantic still wins."""
    a, b, c = sample_memory_triple
    db_session.add_all(
        [
            _make_edge(a, c, origin=EDGE_ORIGIN_HEBBIAN, weight=0.80),
            _make_edge(b, c, origin=EDGE_ORIGIN_SEMANTIC, weight=0.75),
        ]
    )
    await db_session.flush()

    repo, _ = await _merge_b_into_a(db_session, a, b)

    surviving = await repo.get_edge(a.user_id, a.id, c.id)
    assert surviving is not None
    assert (
        surviving.origin == EDGE_ORIGIN_SEMANTIC
    )  # replaces existing hebbian despite lower weight
    assert surviving.weight == pytest.approx(0.75)
