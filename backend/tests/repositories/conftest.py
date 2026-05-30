"""Fixtures for repository tests (Issue #722).

Re-exports sample_memory_pair from tests/neural/conftest.py so it is
discoverable by pytest in this subdirectory, and adds fixtures specific
to neural-edge origin tests.
"""

from uuid import uuid4

import pytest_asyncio

from models.auth import Context, Workspace
from models.memory import (
    EDGE_ORIGIN_HEBBIAN,
    EDGE_ORIGIN_SEMANTIC,
    Memory,
    NeuralMemoryEdge,
)

# Re-export so pytest can discover it in this directory tree.
from tests.neural.conftest import sample_memory_pair  # noqa: F401


@pytest_asyncio.fixture
async def sample_memory_triple(db_session):
    """Create three Memory rows (A, B, C) sharing a Workspace + Context.

    Used by transfer_edges conflict tests (#725): A and B both have an
    outgoing edge to C, so merging B into A forces a (A, C) edge conflict.
    """
    ws = Workspace(
        id=uuid4(),
        name=f"transfer-test-ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id="test_user",
        memory_limit=10000,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db_session.add(ws)
    await db_session.flush()

    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"transfer-test-ctx-{uuid4().hex[:8]}",
        created_by="test_user",
        is_private=False,
    )
    db_session.add(ctx)
    await db_session.flush()

    nodes = [
        Memory(
            id=uuid4(),
            user_id="test_user",
            workspace_id=ws.id,
            context_id=ctx.id,
            summary=f"node {label}",
            content=f"{label} content",
            type="note",
            client="test",
        )
        for label in ("A", "B", "C")
    ]
    db_session.add_all(nodes)
    await db_session.flush()
    return tuple(nodes)


@pytest_asyncio.fixture
async def two_edges_one_hebbian_one_semantic(db_session, sample_memory_pair):  # noqa: F811
    """Create two edges (hebbian + semantic, opposite directions) under a
    unique user_id so per-test bulk operations are immune to leaked data
    from earlier tests.
    """
    from uuid import uuid4

    src, dst = sample_memory_pair
    unique_user = f"edge-isolate-{uuid4().hex[:8]}"
    hebbian = NeuralMemoryEdge(
        user_id=unique_user,
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
        user_id=unique_user,
        src_id=dst.id,  # reversed direction to satisfy unique_edge constraint
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
