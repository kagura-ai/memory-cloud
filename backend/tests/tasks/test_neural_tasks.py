"""Tests for tasks/neural_tasks.py.

Covers weight_decay_task and consolidation_task. Heavy dependencies (DB,
GraphService, MemoryRepository, DecayManager, Qdrant) are patched so tests
run without Docker.

Issue #651 regression guards: weight_decay_task and consolidation_task must
NOT call graph_service.stats() or graph_service.get_node_metrics() — those
methods require workspace_id/context_id which are unavailable in these
global cross-tenant tasks, and the get_stats validator (#273 H-2 / #383)
rejects None pairs to prevent cross-tenant aggregation.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from tasks.neural_tasks import consolidation_task, weight_decay_task
from tests.tasks.conftest import mock_get_db_factory
from utils.datetime import utcnow


def _make_graph(user_id: str = "user-1"):
    """Build a minimal graph_memory row mock with the timestamp fields the task updates."""
    graph = MagicMock()
    graph.user_id = user_id
    graph.last_decay_at = None
    graph.updated_at = None
    return graph


def _make_memory(
    user_id: str = "user-1",
    access_count: int = 0,
    importance: float = 0.0,
    age_days: int = 0,
):
    """Build a minimal Memory row mock with the fields consolidation_task reads."""
    mem = MagicMock()
    mem.id = uuid4()
    mem.user_id = user_id
    mem.access_count = access_count
    mem.importance = importance
    mem.created_at = utcnow() - timedelta(days=age_days)
    return mem


class TestWeightDecayTask:
    @pytest.mark.asyncio
    async def test_skips_when_neural_memory_disabled(self, monkeypatch):
        """ENABLE_NEURAL_MEMORY=false → early return, no DB access.

        Patch ``tasks.neural_tasks.get_db`` (the bound name in the module under
        test) rather than ``db.base.get_db``: the module does
        ``from db.base import get_db`` at import time, copying the reference
        into its own namespace. Patching the source attribute after import
        has no effect on the already-bound name.
        """
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "false")

        with patch("tasks.neural_tasks.get_db") as mock_get_db:
            await weight_decay_task()

        mock_get_db.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_decay_disabled(self, monkeypatch):
        """ENABLE_DECAY=false → early return."""
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("ENABLE_DECAY", "false")

        with patch("tasks.neural_tasks.get_db") as mock_get_db:
            await weight_decay_task()

        mock_get_db.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_call_graph_service_stats(self, monkeypatch):
        """Issue #651 regression guard: weight_decay_task must NOT call
        graph_service.stats() — that call violates 3-level isolation."""
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("ENABLE_DECAY", "true")

        mock_db = MagicMock()
        mock_db.commit = AsyncMock()

        graph = _make_graph()
        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(return_value=[graph])

        mock_graph_service = MagicMock()
        # stats() should NEVER be called; if the regression returns we'll see it here.
        mock_graph_service.stats = AsyncMock(return_value={"total_nodes": 999})

        mock_decay_manager = MagicMock()
        mock_decay_manager.apply_decay = AsyncMock(
            return_value={"edges_decayed": 5, "edges_pruned": 2}
        )

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch(
                "tasks.neural_tasks.NeuralMemoryConfig.from_db", AsyncMock(return_value=MagicMock())
            ),
            patch("tasks.neural_tasks.GraphService", return_value=mock_graph_service),
            patch("tasks.neural_tasks.DecayManager", return_value=mock_decay_manager),
        ):
            await weight_decay_task()

        mock_graph_service.stats.assert_not_called()

    @pytest.mark.asyncio
    async def test_updates_timestamps_when_edges_decayed(self, monkeypatch):
        """When edges_decayed > 0, the task updates last_decay_at and updated_at
        (operational telemetry) but does NOT update the dead total_*/avg/max cache."""
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("ENABLE_DECAY", "true")

        mock_db = MagicMock()
        mock_db.commit = AsyncMock()

        graph = _make_graph()
        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(return_value=[graph])

        mock_decay_manager = MagicMock()
        mock_decay_manager.apply_decay = AsyncMock(
            return_value={"edges_decayed": 3, "edges_pruned": 1}
        )

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch(
                "tasks.neural_tasks.NeuralMemoryConfig.from_db", AsyncMock(return_value=MagicMock())
            ),
            patch("tasks.neural_tasks.GraphService", return_value=MagicMock()),
            patch("tasks.neural_tasks.DecayManager", return_value=mock_decay_manager),
        ):
            await weight_decay_task()

        # Telemetry was refreshed.
        assert graph.last_decay_at is not None
        assert graph.updated_at is not None

    @pytest.mark.asyncio
    async def test_skips_timestamp_update_when_no_edges_decayed(self, monkeypatch):
        """When edges_decayed == 0, last_decay_at is not refreshed."""
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("ENABLE_DECAY", "true")

        mock_db = MagicMock()
        mock_db.commit = AsyncMock()

        graph = _make_graph()
        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(return_value=[graph])

        mock_decay_manager = MagicMock()
        mock_decay_manager.apply_decay = AsyncMock(
            return_value={"edges_decayed": 0, "edges_pruned": 0}
        )

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch(
                "tasks.neural_tasks.NeuralMemoryConfig.from_db", AsyncMock(return_value=MagicMock())
            ),
            patch("tasks.neural_tasks.GraphService", return_value=MagicMock()),
            patch("tasks.neural_tasks.DecayManager", return_value=mock_decay_manager),
        ):
            await weight_decay_task()

        assert graph.last_decay_at is None
        assert graph.updated_at is None


class TestConsolidationTask:
    @pytest.mark.asyncio
    async def test_does_not_construct_graph_service(self):
        """Issue #651 regression guard: consolidation_task must not instantiate
        GraphService at all. Since GraphService is unconstructable in this
        task's context (no workspace_id/context_id), instantiation would be
        the first step toward re-introducing the broken stats() / get_node_metrics()
        calls. Asserting non-instantiation is strictly stronger than asserting
        the methods are not called."""
        mock_db = MagicMock()
        mock_db.commit = AsyncMock()

        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(return_value=[_make_graph()])
        mock_memory_repo = MagicMock()
        mock_memory_repo.list = AsyncMock(return_value=[])

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch("tasks.neural_tasks.MemoryRepository", return_value=mock_memory_repo),
            patch("tasks.neural_tasks.GraphService") as mock_graph_service_cls,
        ):
            await consolidation_task()

        mock_graph_service_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_promotes_frequently_used_memory(self):
        """Promotion criteria still fire on memory-only signals (Issue #1 patterns)."""
        mock_db = MagicMock()
        mock_db.commit = AsyncMock()

        graph = _make_graph()
        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(return_value=[graph])

        # access_count=5 satisfies Pattern 2 (Very frequently used).
        frequent_memory = _make_memory(access_count=5, importance=0.1, age_days=1)
        mock_memory_repo = MagicMock()
        mock_memory_repo.list = AsyncMock(return_value=[frequent_memory])
        mock_memory_repo.promote_to_persistent = AsyncMock()
        mock_memory_repo.delete = AsyncMock()

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch("tasks.neural_tasks.MemoryRepository", return_value=mock_memory_repo),
        ):
            await consolidation_task()

        mock_memory_repo.promote_to_persistent.assert_called_once_with(frequent_memory.id)
        mock_memory_repo.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_deletes_old_unused_memory(self):
        """Deletion criteria still fire on memory-only signals (age>=30 + access_count==0)."""
        mock_db = MagicMock()
        mock_db.commit = AsyncMock()

        graph = _make_graph()
        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(return_value=[graph])

        old_memory = _make_memory(access_count=0, importance=0.0, age_days=45)
        mock_memory_repo = MagicMock()
        mock_memory_repo.list = AsyncMock(return_value=[old_memory])
        mock_memory_repo.promote_to_persistent = AsyncMock()
        mock_memory_repo.delete = AsyncMock()

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch("tasks.neural_tasks.MemoryRepository", return_value=mock_memory_repo),
            patch("db.qdrant.delete_memory_from_qdrant", new=AsyncMock()) as mock_qdrant_delete,
        ):
            await consolidation_task()

        mock_qdrant_delete.assert_called_once_with(old_memory.user_id, old_memory.id)
        mock_memory_repo.delete.assert_called_once_with(old_memory.id)
        mock_memory_repo.promote_to_persistent.assert_not_called()

    @pytest.mark.asyncio
    async def test_leaves_recent_unused_memory_alone(self):
        """A young, unused, unimportant memory is neither promoted nor deleted."""
        mock_db = MagicMock()
        mock_db.commit = AsyncMock()

        graph = _make_graph()
        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(return_value=[graph])

        idle_memory = _make_memory(access_count=0, importance=0.0, age_days=1)
        mock_memory_repo = MagicMock()
        mock_memory_repo.list = AsyncMock(return_value=[idle_memory])
        mock_memory_repo.promote_to_persistent = AsyncMock()
        mock_memory_repo.delete = AsyncMock()

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch("tasks.neural_tasks.MemoryRepository", return_value=mock_memory_repo),
        ):
            await consolidation_task()

        mock_memory_repo.promote_to_persistent.assert_not_called()
        mock_memory_repo.delete.assert_not_called()
