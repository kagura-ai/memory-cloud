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

    # mock_graph from neural/conftest.py

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


class TestActivationClampedToUnitInterval:
    """#1197: spread() must never emit activation > 1.0.

    Edge weights are Hebbian association strengths clipped to
    ``config.weight_max`` (default 3.0), NOT probabilities — but
    ``propagated = src * spread_decay * weight`` was fed verbatim into
    ``ActivationState`` whose ``__post_init__`` hard-requires [0, 1]. A
    reinforced edge whose weight passed ``1 / spread_decay`` (~1.667 at the
    0.6 default) made explore raise
    ``ValueError('activation must be in [0, 1], got 1.004...')`` — the field
    report's 13 crashes over one campaign, value climbing per recall.
    """

    @pytest.fixture
    def node_ids(self):
        return {f"node{i}": str(uuid4()) for i in range(1, 8)}

    def _config(self):
        # Production defaults: spread_decay=0.6, weight_max=3.0. The pairing
        # itself is what makes activation exceed 1.0 for weight > 1.667.
        return NeuralMemoryConfig(spread_hops=1, spread_decay=0.6, spread_threshold=0.01)

    @pytest.mark.asyncio
    async def test_hot_edge_does_not_raise_and_stays_in_range(self, mock_graph, node_ids):
        """A single reinforced edge (weight 1.673 → the ticket's 1.004) must
        not crash spread(), and the neighbour's activation clamps to <= 1.0."""
        spreader = ActivationSpreader(mock_graph, self._config())
        n1, n2 = node_ids["node1"], node_ids["node2"]
        mock_graph.edge_repo.get_outgoing_edges = AsyncMock(
            return_value=[MagicMock(dst_id=n2, weight=1.673)]
        )

        results = await spreader.spread({n1: 1.0}, max_hops=1)

        for r in results:
            assert 0.0 <= r.activation <= 1.0
        n2_result = next(r for r in results if r.node_id == n2)
        assert n2_result.activation == pytest.approx(1.0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("weight", [1.7, 2.0, 3.0])
    async def test_ticket_weights_never_exceed_one(self, mock_graph, node_ids, weight):
        """Reproduce #1197 across the reinforced-weight range up to weight_max:
        every one currently raises; post-fix none raise and all clamp to 1.0."""
        spreader = ActivationSpreader(mock_graph, self._config())
        n1, n2 = node_ids["node1"], node_ids["node2"]
        mock_graph.edge_repo.get_outgoing_edges = AsyncMock(
            return_value=[MagicMock(dst_id=n2, weight=weight)]
        )

        results = await spreader.spread({n1: 1.0}, max_hops=1)

        n2_result = next(r for r in results if r.node_id == n2)
        assert n2_result.activation == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_normal_weight_behaviour_is_unchanged(self, mock_graph, node_ids):
        """The clamp must be a no-op in the normal regime (weight <= 1.667):
        activation stays exactly src * spread_decay * weight."""
        spreader = ActivationSpreader(mock_graph, self._config())
        n1, n2 = node_ids["node1"], node_ids["node2"]
        mock_graph.edge_repo.get_outgoing_edges = AsyncMock(
            return_value=[MagicMock(dst_id=n2, weight=0.9)]
        )

        results = await spreader.spread({n1: 1.0}, max_hops=1)

        n2_result = next(r for r in results if r.node_id == n2)
        # 1.0 * 0.6 * 0.9 = 0.54 — untouched by the clamp.
        assert n2_result.activation == pytest.approx(0.54)

    @pytest.mark.asyncio
    async def test_deep_fanin_stays_crash_safe(self, mock_graph, node_ids):
        """A fan-in hub sums activation across incoming edges (next_layer +=),
        so its running total can exceed 1.0 even with every weight <= 1.0. That
        sum only ever re-enters as the NEXT hop's source (never as an
        ActivationState directly), where the per-edge clamp bounds the product —
        so deep fan-in must stay crash-safe. The accumulation is deliberately
        left UN-clamped (clamping it would shrink deep-hop propagation and
        change non-crashing retrieval results — out of scope for the crash fix),
        so the downstream node reflects the un-capped hub."""
        spreader = ActivationSpreader(mock_graph, self._config())
        seed = node_ids["node1"]
        a, b, c = node_ids["node2"], node_ids["node3"], node_ids["node4"]
        hub = node_ids["node5"]
        downstream = node_ids["node6"]

        async def edges(user_id, src_id, **kwargs):
            s = str(src_id)
            if s == seed:
                return [
                    MagicMock(dst_id=a, weight=1.0),
                    MagicMock(dst_id=b, weight=1.0),
                    MagicMock(dst_id=c, weight=1.0),
                ]
            if s in (a, b, c):
                return [MagicMock(dst_id=hub, weight=1.0)]
            if s == hub:
                return [MagicMock(dst_id=downstream, weight=1.0)]
            return []

        mock_graph.edge_repo.get_outgoing_edges = AsyncMock(side_effect=edges)

        results = await spreader.spread({seed: 1.0}, max_hops=3)

        # No ActivationState escapes [0, 1] — the crash invariant holds.
        for r in results:
            assert 0.0 <= r.activation <= 1.0
        # hub accumulates 3 * (0.6 * 0.6) = 1.08 (un-clamped source); downstream
        # = clamp(1.08 * 0.6 * 1.0) = 0.648. This pins that the fix does NOT cap
        # the accumulator, i.e. non-crashing propagation is preserved.
        ds = next(r for r in results if r.node_id == downstream)
        assert ds.activation == pytest.approx(0.648)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("max_hops", [0, 1])
    async def test_out_of_range_seed_is_clamped_not_crashed(self, mock_graph, node_ids, max_hops):
        """Seeds are a documented [0,1] precondition but callers are not
        validated; an out-of-range seed must clamp, not hit the hard guard —
        both on the max_hops==0 fast path and the main path."""
        spreader = ActivationSpreader(mock_graph, self._config())
        n1 = node_ids["node1"]
        mock_graph.edge_repo.get_outgoing_edges = AsyncMock(return_value=[])

        results = await spreader.spread({n1: 1.5}, max_hops=max_hops)

        seed_result = next(r for r in results if r.node_id == n1)
        assert seed_result.activation == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_out_of_range_seed_clamped_as_propagation_source(self, mock_graph, node_ids):
        """The clamped seed must also drive DOWNSTREAM propagation, not just its
        own stored value: an out-of-range seed whose product stays in range
        (so _clamp01 is a per-edge no-op) must still propagate as if it were 1.0
        — otherwise behaviour would depend on the invalid input magnitude."""
        spreader = ActivationSpreader(mock_graph, self._config())
        n1, n2 = node_ids["node1"], node_ids["node2"]
        mock_graph.edge_repo.get_outgoing_edges = AsyncMock(
            return_value=[MagicMock(dst_id=n2, weight=0.9)]
        )

        results = await spreader.spread({n1: 1.5}, max_hops=1)

        n2_result = next(r for r in results if r.node_id == n2)
        # Clamped seed 1.0 * 0.6 * 0.9 = 0.54 (NOT the raw 1.5 * 0.6 * 0.9 = 0.81).
        assert n2_result.activation == pytest.approx(0.54)
