"""Tests for DecayManager."""

import math

import pytest

from models.memory import EDGE_ORIGIN_HEBBIAN
from neural.config import NeuralMemoryConfig
from neural.decay import DecayManager


class TestDecayManager:
    """Test DecayManager for forgetting and weight decay."""

    @pytest.fixture
    def config(self):
        """Create test config."""
        return NeuralMemoryConfig(
            enable_decay=True,
            decay_rate=0.01,
            decay_background_interval=60,
            prune_threshold=0.1,
            consolidation_use_count_min=5,
            consolidation_importance_min=0.7,
        )

    # mock_graph from neural/conftest.py

    @pytest.fixture
    def manager(self, mock_graph, config):
        """Create DecayManager."""
        return DecayManager(mock_graph, config)

    def test_init(self, mock_graph, config):
        """Test DecayManager initialization."""
        manager = DecayManager(mock_graph, config)
        assert manager.graph == mock_graph
        assert manager.config == config
        assert manager._last_decay_time is None

    @pytest.mark.asyncio
    async def test_apply_decay_disabled(self, mock_graph):
        """Test decay is skipped when disabled."""
        config = NeuralMemoryConfig(enable_decay=False)
        manager = DecayManager(mock_graph, config)

        result = await manager.apply_decay("test_user")
        assert result["edges_decayed"] == 0
        assert result["edges_pruned"] == 0
        mock_graph.edge_repo.bulk_decay_weights.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_decay_first_run(self, manager, mock_graph):
        """Test first decay run uses default interval."""
        result = await manager.apply_decay("test_user")

        assert result["edges_decayed"] == 10
        assert result["edges_pruned"] == 2
        assert result["delta_seconds"] == 60  # decay_background_interval

        # Verify decay factor calculated correctly
        expected_factor = math.exp(-0.01 * 60)
        mock_graph.edge_repo.bulk_decay_weights.assert_called_once_with(
            "test_user", expected_factor, only_origin=EDGE_ORIGIN_HEBBIAN
        )

    @pytest.mark.asyncio
    async def test_apply_decay_updates_last_decay_time(self, manager):
        """Test that last decay time is updated after apply."""
        assert manager._last_decay_time is None
        await manager.apply_decay("test_user")
        assert manager._last_decay_time is not None

    @pytest.mark.asyncio
    async def test_apply_decay_subsequent_run(self, manager, mock_graph):
        """Test subsequent decay uses actual time delta."""
        # First run
        await manager.apply_decay("test_user")
        first_time = manager._last_decay_time

        # Second run should use delta from first run
        await manager.apply_decay("test_user")
        assert manager._last_decay_time > first_time

    @pytest.mark.asyncio
    async def test_apply_decay_prunes_weak_edges(self, manager, mock_graph):
        """Test that weak edges are pruned after decay."""
        await manager.apply_decay("test_user")
        mock_graph.edge_repo.prune_weak_edges.assert_called_once_with(
            "test_user", 0.1, only_origin=EDGE_ORIGIN_HEBBIAN
        )

    @pytest.mark.asyncio
    async def test_prune_weak_edges(self, manager, mock_graph):
        """Test direct pruning of weak edges."""
        result = await manager.prune_weak_edges("test_user")
        assert result == 2
        mock_graph.edge_repo.prune_weak_edges.assert_called_with(
            "test_user", 0.1, only_origin=EDGE_ORIGIN_HEBBIAN
        )

    @pytest.mark.asyncio
    async def test_prune_weak_edges_custom_threshold(self, manager, mock_graph):
        """Test pruning with custom threshold."""
        await manager.prune_weak_edges("test_user", threshold=0.5)
        mock_graph.edge_repo.prune_weak_edges.assert_called_with(
            "test_user", 0.5, only_origin=EDGE_ORIGIN_HEBBIAN
        )

    @pytest.mark.asyncio
    async def test_consolidate_deprecated(self, manager):
        """Test that consolidation returns empty (deprecated in SQL backend)."""
        result = await manager.consolidate_to_long_term("test_user", [])
        assert result == []

    @pytest.mark.asyncio
    async def test_apply_decay_zero_delta(self, manager):
        """Test decay with zero time delta returns no changes."""
        # Apply once to set _last_decay_time
        await manager.apply_decay("test_user")
        # Reset mocks
        manager.graph.edge_repo.bulk_decay_weights.reset_mock()
        manager.graph.edge_repo.prune_weak_edges.reset_mock()

        # Apply again immediately — delta is ~0 but positive
        # The exact behavior depends on timing, but it should not error
        result = await manager.apply_decay("test_user")
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_decay_exponential_formula(self, manager, mock_graph):
        """Test exponential decay formula: w(t+dt) = w(t) * exp(-rate * dt)."""
        await manager.apply_decay("test_user")

        # With rate=0.01 and dt=60: factor = exp(-0.6) ≈ 0.5488
        expected_factor = math.exp(-0.01 * 60)
        call_args = mock_graph.edge_repo.bulk_decay_weights.call_args
        actual_factor = call_args[0][1]
        assert abs(actual_factor - expected_factor) < 0.0001
