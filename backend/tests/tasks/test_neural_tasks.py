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

from services.memory_service import MemoryService
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
    mem.is_pinned = False  # #1523: a bare MagicMock attribute would read as pinned
    mem.is_tool_triggered = False  # same trap for the tool-guardrail exemption
    mem.tags = []  # #2584: no exempt tag
    mem.point_id = mem.id  # #1829: Memory.point_id is a property (falls back to id)
    return mem


def _rows(*memories, resource_owned: bool = False) -> list:
    """What ``list_working_with_resource_owned`` returns for ``memories`` (#2996)."""
    return [(memory, resource_owned) for memory in memories]


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
        mock_db.execute = AsyncMock()  # #1985 graph learning lock

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
        mock_db.execute = AsyncMock()  # #1985 graph learning lock

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
    async def test_takes_each_graph_lock_in_user_order(self, monkeypatch):
        """#1985: each user's edge writes run under the graph learning lock,
        taken in user_id order so two batch tasks cannot lock users in opposite
        orders."""
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("ENABLE_DECAY", "true")
        mock_db = MagicMock()
        mock_db.commit = AsyncMock()
        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(
            return_value=[_make_graph("user-b"), _make_graph("user-a")]
        )
        mock_decay_manager = MagicMock()
        mock_decay_manager.apply_decay = AsyncMock(return_value={"edges_decayed": 0})
        lock = AsyncMock()

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch(
                "tasks.neural_tasks.NeuralMemoryConfig.from_db", AsyncMock(return_value=MagicMock())
            ),
            patch("tasks.neural_tasks.GraphService", return_value=MagicMock()),
            patch("tasks.neural_tasks.DecayManager", return_value=mock_decay_manager),
            patch("tasks.neural_tasks.lock_graph_learning", lock),
        ):
            await weight_decay_task()

        assert [c.args for c in lock.await_args_list] == [
            (mock_db, "user-a"),
            (mock_db, "user-b"),
        ]

    @pytest.mark.asyncio
    async def test_skips_timestamp_update_when_no_edges_decayed(self, monkeypatch):
        """When edges_decayed == 0, last_decay_at is not refreshed."""
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("ENABLE_DECAY", "true")

        mock_db = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.execute = AsyncMock()  # #1985 graph learning lock

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
        mock_db.execute = AsyncMock()

        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(return_value=[_make_graph()])
        mock_memory_repo = MagicMock()
        mock_memory_repo.list_working_with_resource_owned = AsyncMock(return_value=[])

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
        mock_db.execute = AsyncMock()

        graph = _make_graph()
        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(return_value=[graph])

        # access_count=5 satisfies Pattern 2 (Very frequently used).
        frequent_memory = _make_memory(access_count=5, importance=0.1, age_days=1)
        mock_memory_repo = MagicMock()
        mock_memory_repo.list_working_with_resource_owned = AsyncMock(
            return_value=_rows(frequent_memory)
        )
        mock_memory_repo.bulk_promote_to_persistent = AsyncMock(side_effect=list)
        mock_memory_repo.delete = AsyncMock()

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch("tasks.neural_tasks.MemoryRepository", return_value=mock_memory_repo),
        ):
            await consolidation_task()

        mock_memory_repo.bulk_promote_to_persistent.assert_awaited_once_with([frequent_memory.id])
        mock_memory_repo.delete.assert_not_called()

    @staticmethod
    async def _run(*memories, monkeypatch=None):
        """Run the task over ``memories`` (one user); return the repo mock and result."""
        mock_db = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.execute = AsyncMock()

        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(return_value=[_make_graph()])
        mock_memory_repo = MagicMock()
        mock_memory_repo.list_working_with_resource_owned = AsyncMock(return_value=_rows(*memories))
        mock_memory_repo.bulk_promote_to_persistent = AsyncMock(side_effect=list)
        mock_memory_repo.delete = AsyncMock()
        mock_memory_repo.soft_delete = AsyncMock()

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch("tasks.neural_tasks.MemoryRepository", return_value=mock_memory_repo),
            patch("db.qdrant.delete_memory_from_qdrant", new=AsyncMock()) as mock_qdrant_delete,
            patch.object(MemoryService, "_queue_forgotten_points", AsyncMock()) as queue,
            patch.object(MemoryService, "delete_forgotten_points", AsyncMock()) as delete_points,
        ):
            result = await consolidation_task()

        # #3021: nothing is deleted, soft or hard, and no point is queued.
        mock_memory_repo.delete.assert_not_called()
        mock_memory_repo.soft_delete.assert_not_called()
        mock_qdrant_delete.assert_not_called()
        queue.assert_not_called()
        delete_points.assert_not_called()
        mock_db.commit.assert_awaited_once()
        return mock_memory_repo, result

    @pytest.mark.asyncio
    async def test_keeps_an_old_never_returned_memory(self, monkeypatch):
        """#3021 acceptance: with Sleep off (the only time this task runs), a
        30-day-old working memory that no recall ever returned survives the
        task; the result keeps its ``deleted`` key at 0."""
        monkeypatch.setenv("SLEEP_ENABLED", "false")
        old = _make_memory(access_count=0, importance=0.0, age_days=30)
        older = _make_memory(access_count=0, importance=0.0, age_days=400)

        repo, result = await self._run(old, older)

        repo.bulk_promote_to_persistent.assert_not_called()
        assert result == {"users": 1, "promoted": 0, "deleted": 0, "resource_skipped": 0}

    @pytest.mark.asyncio
    async def test_still_promotes_by_rule_next_to_a_kept_memory(self, monkeypatch):
        """Rule promotion is unchanged: the used row is promoted, the unused
        old row stays working."""
        monkeypatch.setenv("SLEEP_ENABLED", "false")
        unused = _make_memory(access_count=0, importance=0.0, age_days=45)
        used = _make_memory(access_count=1, importance=0.0, age_days=45)

        repo, result = await self._run(unused, used)

        repo.bulk_promote_to_persistent.assert_awaited_once_with([used.id])
        assert result is not None and result["promoted"] == 1 and result["deleted"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("marker", ["is_pinned", "is_tool_triggered", "open_memo"])
    async def test_keeps_pinned_guardrail_and_memo_rows(self, monkeypatch, marker):
        """The rows the old delete exempted are kept too, like every other row."""
        monkeypatch.setenv("SLEEP_ENABLED", "false")
        memory = _make_memory(access_count=0, importance=0.0, age_days=45)
        if marker == "open_memo":
            memory.tags = ["tech-debt", "status:open"]
        else:
            setattr(memory, marker, True)

        repo, _result = await self._run(memory)

        repo.bulk_promote_to_persistent.assert_not_called()

    @pytest.mark.asyncio
    async def test_leaves_recent_unused_memory_alone(self):
        """A young, unused, unimportant memory is neither promoted nor deleted."""
        mock_db = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.execute = AsyncMock()

        graph = _make_graph()
        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(return_value=[graph])

        idle_memory = _make_memory(access_count=0, importance=0.0, age_days=1)
        mock_memory_repo = MagicMock()
        mock_memory_repo.list_working_with_resource_owned = AsyncMock(
            return_value=_rows(idle_memory)
        )
        mock_memory_repo.bulk_promote_to_persistent = AsyncMock(side_effect=list)
        mock_memory_repo.delete = AsyncMock()

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch("tasks.neural_tasks.MemoryRepository", return_value=mock_memory_repo),
        ):
            await consolidation_task()

        mock_memory_repo.bulk_promote_to_persistent.assert_not_called()
        mock_memory_repo.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_leaves_a_resource_owned_memory_to_its_source(self):
        """#2996: the flag the fetch read with the row (``resource_owned_predicate``)
        decides: an old unused row is not deleted, a frequently used one is not
        promoted, and both are counted."""
        mock_db = MagicMock()
        mock_db.commit = AsyncMock()
        mock_db.execute = AsyncMock()

        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(return_value=[_make_graph()])

        old = _make_memory(access_count=0, importance=0.0, age_days=45)
        used = _make_memory(access_count=9, importance=0.9, age_days=45)
        mock_memory_repo = MagicMock()
        mock_memory_repo.list_working_with_resource_owned = AsyncMock(
            return_value=_rows(old, used, resource_owned=True)
        )
        mock_memory_repo.bulk_promote_to_persistent = AsyncMock(side_effect=list)
        mock_memory_repo.delete = AsyncMock()

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch("tasks.neural_tasks.MemoryRepository", return_value=mock_memory_repo),
        ):
            result = await consolidation_task()

        mock_memory_repo.bulk_promote_to_persistent.assert_not_called()
        mock_memory_repo.delete.assert_not_called()
        assert result is not None and result["resource_skipped"] == 2


class TestGraphPaging:
    """#2385: both tasks cover every graph, one id-ordered page per transaction.

    ``GraphRepository.list`` orders by id; the tasks used to read only its
    first 1000 rows, so with the fixed order every later graph would never be
    decayed or consolidated.
    """

    @staticmethod
    def _recording_db():
        """A session mock whose commits land in the same log as the locks."""
        events: list[tuple[str, ...]] = []
        mock_db = MagicMock()
        mock_db.execute = AsyncMock()
        mock_db.commit = AsyncMock(side_effect=lambda: events.append(("commit",)))

        async def lock(_db, user_id):
            events.append(("lock", user_id))

        return mock_db, events, lock

    @pytest.mark.asyncio
    async def test_weight_decay_pages_through_every_graph(self, monkeypatch):
        """Pages follow each other by skip, each page's users are locked in
        user_id order and committed before the next page is read."""
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("ENABLE_DECAY", "true")
        monkeypatch.setattr("tasks.neural_tasks.GRAPH_PAGE_SIZE", 2)
        mock_db, events, lock = self._recording_db()
        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(
            side_effect=[
                [_make_graph("user-d"), _make_graph("user-c")],
                [_make_graph("user-a")],
            ]
        )
        mock_decay_manager = MagicMock()
        mock_decay_manager.apply_decay = AsyncMock(return_value={"edges_decayed": 0})

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch(
                "tasks.neural_tasks.NeuralMemoryConfig.from_db", AsyncMock(return_value=MagicMock())
            ),
            patch("tasks.neural_tasks.GraphService", return_value=MagicMock()),
            patch("tasks.neural_tasks.DecayManager", return_value=mock_decay_manager),
            patch("tasks.neural_tasks.lock_graph_learning", lock),
        ):
            await weight_decay_task()

        assert [c.kwargs for c in mock_graph_repo.list.await_args_list] == [
            {"skip": 0, "limit": 2},
            {"skip": 2, "limit": 2},
        ]
        assert events == [
            ("lock", "user-c"),
            ("lock", "user-d"),
            ("commit",),
            ("lock", "user-a"),
            ("commit",),
        ]
        assert [c.args[0] for c in mock_decay_manager.apply_decay.await_args_list] == [
            "user-c",
            "user-d",
            "user-a",
        ]

    @pytest.mark.asyncio
    async def test_weight_decay_stops_on_an_empty_page(self, monkeypatch):
        """A full last page is followed by one more read that comes back empty."""
        monkeypatch.setenv("ENABLE_NEURAL_MEMORY", "true")
        monkeypatch.setenv("ENABLE_DECAY", "true")
        monkeypatch.setattr("tasks.neural_tasks.GRAPH_PAGE_SIZE", 2)
        mock_db, events, lock = self._recording_db()
        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(
            side_effect=[[_make_graph("user-a"), _make_graph("user-b")], []]
        )
        mock_decay_manager = MagicMock()
        mock_decay_manager.apply_decay = AsyncMock(return_value={"edges_decayed": 0})

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch(
                "tasks.neural_tasks.NeuralMemoryConfig.from_db", AsyncMock(return_value=MagicMock())
            ),
            patch("tasks.neural_tasks.GraphService", return_value=MagicMock()),
            patch("tasks.neural_tasks.DecayManager", return_value=mock_decay_manager),
            patch("tasks.neural_tasks.lock_graph_learning", lock),
        ):
            await weight_decay_task()

        assert mock_graph_repo.list.await_count == 2
        assert events == [("lock", "user-a"), ("lock", "user-b"), ("commit",)]

    @pytest.mark.asyncio
    async def test_consolidation_pages_through_every_graph(self, monkeypatch):
        """Every user past the first page is consolidated too, one commit per page."""
        monkeypatch.setattr("tasks.neural_tasks.GRAPH_PAGE_SIZE", 2)
        mock_db, events, lock = self._recording_db()
        mock_graph_repo = MagicMock()
        mock_graph_repo.list = AsyncMock(
            side_effect=[
                [_make_graph("user-d"), _make_graph("user-c")],
                [_make_graph("user-a")],
            ]
        )
        mock_memory_repo = MagicMock()
        mock_memory_repo.list_working_with_resource_owned = AsyncMock(return_value=[])

        with (
            patch("tasks.neural_tasks.get_db", mock_get_db_factory(mock_db)),
            patch("tasks.neural_tasks.GraphRepository", return_value=mock_graph_repo),
            patch("tasks.neural_tasks.MemoryRepository", return_value=mock_memory_repo),
            patch("tasks.neural_tasks.lock_graph_learning", lock),
        ):
            await consolidation_task()

        assert [c.kwargs for c in mock_graph_repo.list.await_args_list] == [
            {"skip": 0, "limit": 2},
            {"skip": 2, "limit": 2},
        ]
        # #3021: the task writes no edge (it deletes nothing, so nothing
        # cascades to edge rows): it takes no graph learning lock.
        assert events == [("commit",), ("commit",)]
        assert [
            c.kwargs["user_id"]
            for c in mock_memory_repo.list_working_with_resource_owned.await_args_list
        ] == [
            "user-c",
            "user-d",
            "user-a",
        ]
