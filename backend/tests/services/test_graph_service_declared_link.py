"""Tests for GraphService.add_edge declared_link protection (#457).

GraphService.add_edge is the entry point for all automated writers
(Hebbian co-activation via HebbianLearner._apply_update_to_edge → graph.add_edge).
It must always pass protect_declared_link=True so user-declared links survive
co-activation retyping.
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
