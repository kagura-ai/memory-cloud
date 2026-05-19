"""Repository contract tests for edge.origin plumbing (Issue #722).

Verifies:
- create_or_update_edge writes the supplied origin value
- create_or_update_edge defaults to EDGE_ORIGIN_HEBBIAN
- create_edge_if_absent writes the supplied origin value
- bulk_decay_weights(only_origin=...) skips non-matching edges
- prune_weak_edges(only_origin=...) skips non-matching edges
"""

import pytest

from models.memory import (
    EDGE_ORIGIN_HEBBIAN,
    EDGE_ORIGIN_SEMANTIC,
)
from repositories.neural_edge import NeuralEdgeRepository


@pytest.mark.asyncio
async def test_create_or_update_edge_writes_origin(db_session, sample_memory_pair):
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="related_to",
        weight=0.8,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_SEMANTIC,
    )
    edge = await repo.get_edge(src.user_id, src.id, dst.id)
    assert edge is not None
    assert edge.origin == EDGE_ORIGIN_SEMANTIC


@pytest.mark.asyncio
async def test_create_or_update_edge_default_origin_is_hebbian(db_session, sample_memory_pair):
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="neural_association",
        weight=0.1,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
    )
    edge = await repo.get_edge(src.user_id, src.id, dst.id)
    assert edge is not None
    assert edge.origin == EDGE_ORIGIN_HEBBIAN


@pytest.mark.asyncio
async def test_create_edge_if_absent_writes_origin(db_session, sample_memory_pair):
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    edge = await repo.create_edge_if_absent(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="related_to",
        weight=0.7,
        confidence=0.9,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_SEMANTIC,
    )
    assert edge is not None
    assert edge.origin == EDGE_ORIGIN_SEMANTIC


@pytest.mark.asyncio
async def test_bulk_decay_only_origin_hebbian_skips_semantic(
    db_session, two_edges_one_hebbian_one_semantic
):
    hebbian, semantic = two_edges_one_hebbian_one_semantic
    repo = NeuralEdgeRepository(db_session)
    user_id = hebbian.user_id

    initial_h_weight = hebbian.weight
    initial_s_weight = semantic.weight

    n = await repo.bulk_decay_weights(user_id, decay_factor=0.5, only_origin=EDGE_ORIGIN_HEBBIAN)
    # flush so ORM-layer weight updates are persisted before refresh re-reads from DB
    await db_session.flush()
    await db_session.refresh(hebbian)
    await db_session.refresh(semantic)

    assert n == 1
    assert hebbian.weight == pytest.approx(initial_h_weight * 0.5)
    assert semantic.weight == initial_s_weight  # untouched


@pytest.mark.asyncio
async def test_prune_only_origin_hebbian_skips_semantic(
    db_session, two_edges_one_hebbian_one_semantic
):
    hebbian, semantic = two_edges_one_hebbian_one_semantic
    user_id = hebbian.user_id
    # Force both below the threshold the test will use
    hebbian.weight = 0.001
    semantic.weight = 0.001
    await db_session.flush()

    repo = NeuralEdgeRepository(db_session)
    deleted = await repo.prune_weak_edges(
        user_id, weight_threshold=0.01, only_origin=EDGE_ORIGIN_HEBBIAN
    )
    assert deleted == 1

    surviving = await repo.get_edge(user_id, semantic.src_id, semantic.dst_id)
    assert surviving is not None
    assert surviving.origin == EDGE_ORIGIN_SEMANTIC


@pytest.mark.asyncio
async def test_upsert_does_not_demote_semantic_to_hebbian(db_session, sample_memory_pair):
    """Hebbian co-recall must NOT downgrade an existing semantic edge."""
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    # First write: semantic edge
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="related_to",
        weight=0.8,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_SEMANTIC,
    )

    # Second write: Hebbian co-recall upsert with default origin
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="neural_association",
        weight=0.5,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        # origin omitted — defaults to EDGE_ORIGIN_HEBBIAN
    )

    edge = await repo.get_edge(src.user_id, src.id, dst.id)
    assert edge is not None
    assert edge.origin == EDGE_ORIGIN_SEMANTIC  # preserved, not demoted


@pytest.mark.asyncio
async def test_upsert_promotes_hebbian_to_semantic(db_session, sample_memory_pair):
    """Sleep edge_discovery upsert MUST be able to promote a hebbian edge to semantic."""
    src, dst = sample_memory_pair
    repo = NeuralEdgeRepository(db_session)

    # First write: Hebbian edge (default)
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="neural_association",
        weight=0.05,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
    )

    # Second write: sleep edge_discovery upsert with semantic origin
    await repo.create_or_update_edge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        edge_type="related_to",
        weight=0.85,
        confidence=1.0,
        workspace_id=str(src.workspace_id),
        context_id=str(src.context_id),
        origin=EDGE_ORIGIN_SEMANTIC,
    )

    edge = await repo.get_edge(src.user_id, src.id, dst.id)
    assert edge is not None
    assert edge.origin == EDGE_ORIGIN_SEMANTIC  # promoted
