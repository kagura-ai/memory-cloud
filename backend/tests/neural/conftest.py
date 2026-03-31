"""Shared fixtures for neural module tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest


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
