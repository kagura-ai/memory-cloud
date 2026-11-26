"""Tests for GraphService.add_edge declared_link protection (#457).

GraphService.add_edge is the shared upsert for automated writers. It must
always pass protect_declared_link=True so user-declared links survive
automated retyping. Hebbian co-activation writes through add_hebbian_edge /
remove_hebbian_edge instead (#3018), which carry their own rule.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.graph_service import GraphService


@pytest.mark.asyncio
async def test_add_edge_passes_protect_declared_link_true():
    """GraphService.add_edge always sets protect_declared_link=True.

    This pins the contract: any caller that goes through GraphService
    (Hebbian, anything else built on add_edge) gets declared_link
    protection automatically. User-driven update_edge bypasses this
    by calling NeuralEdgeRepository.create_or_update_edge directly.
    """
    src_id = uuid4()
    dst_id = uuid4()

    mock_db = MagicMock()
    mock_edge_repo = MagicMock()
    mock_edge_repo.create_or_update_edge = AsyncMock()

    graph = GraphService.__new__(GraphService)
    graph.user_id = "test_user_457"
    graph.workspace_id = str(uuid4())
    graph.context_id = str(uuid4())
    graph.db = mock_db
    graph.edge_repo = mock_edge_repo

    await graph.add_edge(
        src_id=src_id,
        dst_id=dst_id,
        rel_type="neural_association",
        weight=1.03,
    )

    mock_edge_repo.create_or_update_edge.assert_awaited_once()
    kwargs = mock_edge_repo.create_or_update_edge.await_args.kwargs
    assert kwargs["protect_declared_link"] is True, (
        "GraphService.add_edge must always pass protect_declared_link=True "
        "so Hebbian co-activation cannot retype declared_link edges."
    )
    assert kwargs["edge_type"] == "neural_association"
    assert kwargs["src_id"] == src_id
    assert kwargs["dst_id"] == dst_id


def _graph_with_repo(repo: MagicMock) -> GraphService:
    graph = GraphService.__new__(GraphService)
    graph.user_id = "test_user_3018"
    graph.workspace_id = str(uuid4())
    graph.context_id = str(uuid4())
    graph.db = MagicMock()
    graph.edge_repo = repo
    return graph


@pytest.mark.asyncio
async def test_add_hebbian_edge_uses_the_hebbian_upsert_not_the_shared_one():
    """#3018: Hebbian learning has its own rule in the repository; add_edge
    (and its protect_declared_link contract above) is left to other writers."""
    repo = MagicMock()
    repo.upsert_hebbian_edge = AsyncMock(return_value=False)
    repo.create_or_update_edge = AsyncMock()
    graph = _graph_with_repo(repo)
    src_id, dst_id = uuid4(), uuid4()

    written = await graph.add_hebbian_edge(
        str(src_id), dst_id, weight=0.7, edge_metadata={"created_by": "hebbian_learning"}
    )

    assert written is False
    repo.create_or_update_edge.assert_not_called()
    repo.upsert_hebbian_edge.assert_awaited_once_with(
        user_id="test_user_3018",
        src_id=src_id,
        dst_id=dst_id,
        weight=0.7,
        confidence=1.0,
        edge_metadata={"created_by": "hebbian_learning"},
        workspace_id=graph.workspace_id,
        context_id=graph.context_id,
    )


@pytest.mark.asyncio
async def test_remove_hebbian_edge_uses_the_guarded_prune():
    repo = MagicMock()
    repo.prune_hebbian_edge = AsyncMock(return_value=True)
    repo.delete_edge = AsyncMock()
    graph = _graph_with_repo(repo)
    src_id, dst_id = uuid4(), uuid4()

    assert await graph.remove_hebbian_edge(src_id, str(dst_id), weight=0.004) is True

    repo.delete_edge.assert_not_called()
    repo.prune_hebbian_edge.assert_awaited_once_with(
        "test_user_3018",
        src_id,
        dst_id,
        weight=0.004,
        workspace_id=graph.workspace_id,
        context_id=graph.context_id,
    )
