"""Tests for ActivationSpreader."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from neural.activation import ActivationSpreader
from neural.config import NeuralMemoryConfig


class TestActivationSpreader:
    """Test ActivationSpreader graph-based activation propagation."""

    @pytest.fixture
    def config(self):
        """Create test config."""
        return NeuralMemoryConfig(
            spread_hops=2,
            spread_decay=0.8,
            spread_threshold=0.1,
        )

    @pytest.fixture
    def node_ids(self):
        """Generate valid UUID node IDs."""
        return {f"node{i}": str(uuid4()) for i in range(1, 6)}

    @pytest.fixture
    def mock_graph(self):
        """Create mock graph service with SQL backend."""
        graph = MagicMock()
        graph.user_id = "test_user"
        graph.workspace_id = None
        graph.context_id = None
        graph.edge_repo = MagicMock()
        graph.edge_repo.get_outgoing_edges = AsyncMock(return_value=[])
        return graph

    @pytest.fixture
    def spreader(self, mock_graph, config):
        """Create ActivationSpreader."""
        return ActivationSpreader(mock_graph, config)

    def test_init(self, mock_graph, config):
        """Test ActivationSpreader initialization."""
        spreader = ActivationSpreader(mock_graph, config)
        assert spreader.graph == mock_graph
        assert spreader.config == config

    @pytest.mark.asyncio
    async def test_spread_zero_hops(self, spreader, node_ids):
        """Test spreading with max_hops=0 (no propagation)."""
        n1, n2 = node_ids["node1"], node_ids["node2"]
        seed_activations = {n1: 1.0, n2: 0.8}
        results = await spreader.spread(seed_activations, max_hops=0)

        assert len(results) == 2
        result_ids = {r.node_id for r in results}
        assert n1 in result_ids
        assert n2 in result_ids

        for result in results:
            assert result.activation == seed_activations[result.node_id]
            assert result.hop == 0

    @pytest.mark.asyncio
    async def test_spread_one_hop(self, spreader, mock_graph, node_ids):
        """Test spreading with 1 hop."""
        n1, n2, n4 = node_ids["node1"], node_ids["node2"], node_ids["node4"]

        edge_to_2 = MagicMock(dst_id=n2, weight=0.9)
        edge_to_4 = MagicMock(dst_id=n4, weight=0.5)
        mock_graph.edge_repo.get_outgoing_edges = AsyncMock(return_value=[edge_to_2, edge_to_4])

        seed_activations = {n1: 1.0}
        results = await spreader.spread(seed_activations, max_hops=1)

        result_ids = {r.node_id for r in results}
        assert n1 in result_ids
        assert n2 in result_ids
        assert n4 in result_ids

        # activation = 1.0 * 0.8 (decay) * 0.9 (weight) = 0.72
        n2_result = next(r for r in results if r.node_id == n2)
        assert abs(n2_result.activation - 0.72) < 0.001
        assert n2_result.hop == 1

        n4_result = next(r for r in results if r.node_id == n4)
        assert abs(n4_result.activation - 0.4) < 0.001

    @pytest.mark.asyncio
    async def test_spread_threshold_filtering(self, spreader, mock_graph, node_ids):
        """Test that activations below threshold are filtered."""
        spreader.config.spread_threshold = 0.5
        n1, n2, n4 = node_ids["node1"], node_ids["node2"], node_ids["node4"]

        edge_to_2 = MagicMock(dst_id=n2, weight=0.9)
        edge_to_4 = MagicMock(dst_id=n4, weight=0.5)
        mock_graph.edge_repo.get_outgoing_edges = AsyncMock(return_value=[edge_to_2, edge_to_4])

        results = await spreader.spread({n1: 1.0}, max_hops=1)
        result_ids = {r.node_id for r in results}
        # n2: 0.72 > 0.5
        assert n2 in result_ids
        # n4: 0.4 < 0.5
        assert n4 not in result_ids

    @pytest.mark.asyncio
    async def test_spread_results_sorted(self, spreader, mock_graph, node_ids):
        """Test that results are sorted by activation (descending)."""
        n1, n2 = node_ids["node1"], node_ids["node2"]
        edge = MagicMock(dst_id=n2, weight=0.9)
        mock_graph.edge_repo.get_outgoing_edges = AsyncMock(return_value=[edge])

        results = await spreader.spread({n1: 1.0}, max_hops=1)
        for i in range(len(results) - 1):
            assert results[i].activation >= results[i + 1].activation

    @pytest.mark.asyncio
    async def test_spread_no_neighbors(self, spreader, node_ids):
        """Test spreading when node has no neighbors."""
        n1 = node_ids["node1"]
        results = await spreader.spread({n1: 1.0}, max_hops=2)
        assert len(results) == 1
        assert results[0].node_id == n1
        assert results[0].activation == 1.0

    @pytest.mark.asyncio
    async def test_spread_multiple_seeds(self, spreader, mock_graph, node_ids):
        """Test spreading from multiple seed nodes."""
        n1, n2, n3 = node_ids["node1"], node_ids["node2"], node_ids["node3"]
        edge = MagicMock(dst_id=n3, weight=0.7)

        async def mock_edges(user_id, src_id, **kwargs):
            if str(src_id) == n2:
                return [edge]
            return []

        mock_graph.edge_repo.get_outgoing_edges = AsyncMock(side_effect=mock_edges)

        results = await spreader.spread({n1: 1.0, n2: 0.5}, max_hops=1)
        result_ids = {r.node_id for r in results}
        assert n1 in result_ids
        assert n2 in result_ids
        assert n3 in result_ids

    @pytest.mark.asyncio
    async def test_spread_two_hops(self, spreader, mock_graph, node_ids):
        """Test spreading reaches 2 hops."""
        n1, n2, n3 = node_ids["node1"], node_ids["node2"], node_ids["node3"]
        edge_1_2 = MagicMock(dst_id=n2, weight=0.9)
        edge_2_3 = MagicMock(dst_id=n3, weight=0.7)

        async def mock_edges(user_id, src_id, **kwargs):
            if str(src_id) == n1:
                return [edge_1_2]
            if str(src_id) == n2:
                return [edge_2_3]
            return []

        mock_graph.edge_repo.get_outgoing_edges = AsyncMock(side_effect=mock_edges)

        results = await spreader.spread({n1: 1.0}, max_hops=2)
        result_ids = {r.node_id for r in results}
        assert n3 in result_ids

        # n3: (1.0 * 0.8 * 0.9) * 0.8 * 0.7 = 0.4032
        n3_result = next(r for r in results if r.node_id == n3)
        assert abs(n3_result.activation - 0.4032) < 0.001
        assert n3_result.hop == 2
