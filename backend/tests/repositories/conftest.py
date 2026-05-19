"""Fixtures for repository tests (Issue #722).

Re-exports sample_memory_pair from tests/neural/conftest.py so it is
discoverable by pytest in this subdirectory, and adds fixtures specific
to neural-edge origin tests.
"""

import pytest_asyncio

from models.memory import (
    EDGE_ORIGIN_HEBBIAN,
    EDGE_ORIGIN_SEMANTIC,
    NeuralMemoryEdge,
)

# Re-export so pytest can discover it in this directory tree.
from tests.neural.conftest import sample_memory_pair  # noqa: F401


@pytest_asyncio.fixture
async def two_edges_one_hebbian_one_semantic(db_session, sample_memory_pair):
    """Create two edges: one hebbian, one semantic, in opposite directions.

    Uses opposite src/dst direction so both satisfy the unique_edge constraint
    (user_id, src_id, dst_id) which does not allow duplicate (src, dst) pairs.
    """
    src, dst = sample_memory_pair
    hebbian = NeuralMemoryEdge(
        user_id=src.user_id,
        src_id=src.id,
        dst_id=dst.id,
        workspace_id=src.workspace_id,
        context_id=src.context_id,
        edge_type="neural_association",
        weight=0.5,
        confidence=1.0,
        origin=EDGE_ORIGIN_HEBBIAN,
    )
    semantic = NeuralMemoryEdge(
        user_id=src.user_id,
        src_id=dst.id,  # opposite direction — unique_edge constraint safe
        dst_id=src.id,
        workspace_id=src.workspace_id,
        context_id=src.context_id,
        edge_type="related_to",
        weight=0.7,
        confidence=1.0,
        origin=EDGE_ORIGIN_SEMANTIC,
    )
    db_session.add_all([hebbian, semantic])
    await db_session.flush()
    return hebbian, semantic
