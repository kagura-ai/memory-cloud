"""Shared fixtures for neural module tests."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio

from models.auth import Context, Workspace
from models.memory import Memory


@pytest.fixture
def mock_graph():
    """Create mock GraphService with SQL backend.

    Provides a base mock with common attributes.
    Individual tests can override specific methods as needed.
    """
    graph = MagicMock()
    graph.user_id = "test_user"
    graph.workspace_id = None
    graph.context_id = None
    graph.db = MagicMock()
    graph.edge_repo = MagicMock()
    graph.edge_repo.get_outgoing_edges = AsyncMock(return_value=[])
    graph.edge_repo.bulk_decay_weights = AsyncMock(return_value=10)
    graph.edge_repo.prune_weak_edges = AsyncMock(return_value=2)
    graph.edge_repo.get_edge_weight = AsyncMock(return_value=0.5)
    graph.edge_repo.update_edge_weight = AsyncMock(return_value=True)
    graph.edge_repo.create_edge = AsyncMock()
    graph.edge_repo.get_outgoing_edges_count = AsyncMock(return_value=5)
    graph.edge_repo.prune_weakest_edges = AsyncMock(return_value=0)
    graph.get_edge = AsyncMock(return_value={"weight": 0.5})
    graph.has_edge = AsyncMock(return_value=True)
    graph.remove_edge = AsyncMock()
    graph.update_edge = AsyncMock()
    return graph


@pytest_asyncio.fixture
async def sample_memory_pair(db_session):
    """Create two Memory rows sharing a Workspace + Context for edge constraint tests.

    The two memories share workspace_id and context_id, which is required by
    the ``ck_neural_memory_edges_ws_ctx_not_null`` CHECK constraint on edges.
    """
    ws = Workspace(
        id=uuid4(),
        name=f"neural-test-ws-{uuid4().hex[:8]}",
        plan_name="free",
        owner_user_id="test_user",
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db_session.add(ws)
    await db_session.flush()

    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"neural-test-ctx-{uuid4().hex[:8]}",
        created_by="test_user",
        is_private=False,
    )
    db_session.add(ctx)
    await db_session.flush()

    src = Memory(
        id=uuid4(),
        user_id="test_user",
        workspace_id=ws.id,
        context_id=ctx.id,
        summary="source memory",
        content="src content",
        type="note",
        client="test",
    )
    dst = Memory(
        id=uuid4(),
        user_id="test_user",
        workspace_id=ws.id,
        context_id=ctx.id,
        summary="destination memory",
        content="dst content",
        type="note",
        client="test",
    )
    db_session.add_all([src, dst])
    await db_session.flush()
    return src, dst
