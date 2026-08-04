"""Tests for tasks/embedding_tasks.py.

Covers sweep logic and scheduler registration.  All heavy dependencies
(DB, embedding service) are patched so tests run without Docker.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from tasks.embedding_tasks import schedule_embedding_tasks, sweep_pending_embeddings
from tests.tasks.conftest import mock_get_db_factory


class TestScheduleEmbeddingTasks:
    def test_registers_job_with_correct_interval(self):
        scheduler = MagicMock()
        schedule_embedding_tasks(scheduler)

        scheduler.add_job.assert_called_once()
        call = scheduler.add_job.call_args
        assert call.kwargs["id"] == "sweep_pending_embeddings"
        assert call.kwargs["name"] == "Sweep Pending Embeddings"
        assert call.kwargs["replace_existing"] is True
        # IntervalTrigger(seconds=30)
        trigger = call.kwargs["trigger"]
        assert trigger.interval.total_seconds() == 30


class TestSweepPendingEmbeddings:
    @pytest.mark.asyncio
    async def test_no_stale_memories_exits_early(self):
        """When the candidate query returns nothing, the sweep does no work.

        #1496 added a backlog count that runs BEFORE the candidate query — on
        purpose, because "nothing to sweep" is exactly the state that needs
        reporting when every failed row has gone terminal. So the sweep now
        issues two statements and still processes nothing.
        """
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        # The backlog count reads .one(); a healthy deployment reports zero.
        mock_result.one.return_value = SimpleNamespace(unsearchable=0, stalled=0)
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch("db.base.get_db", mock_get_db_factory(mock_db)),
            patch("services.memory_service.process_pending_embedding", new=AsyncMock()) as proc,
        ):
            await sweep_pending_embeddings()

        assert mock_db.execute.await_count == 2, (
            "expected the #1496 backlog count plus the candidate query"
        )
        proc.assert_not_called()

    @pytest.mark.asyncio
    async def test_reports_the_unsearchable_backlog(self):
        """The signal #1496 exists for.

        A failed embedding never reaches Qdrant, and BM25 lives there too, so
        these rows are missing from recall in both modes while still being
        counted and billed. Nothing surfaced that before; the only way to find
        it was to query the database by hand.
        """
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_result.one.return_value = SimpleNamespace(unsearchable=467, stalled=467)
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("db.base.get_db", mock_get_db_factory(mock_db)):
            with patch("tasks.embedding_tasks.logger") as log:
                await sweep_pending_embeddings()

        warnings = [c for c in log.warning.call_args_list if c.args[0] == "embedding_unsearchable_backlog"]
        assert warnings, "the backlog was not reported"
        assert warnings[0].kwargs == {"unsearchable": 467, "stalled": 467}

    @pytest.mark.asyncio
    async def test_a_healthy_deployment_stays_quiet(self):
        """Absence of the line has to mean something, or it is not a signal."""
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_result.one.return_value = SimpleNamespace(unsearchable=0, stalled=0)
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("db.base.get_db", mock_get_db_factory(mock_db)):
            with patch("tasks.embedding_tasks.logger") as log:
                await sweep_pending_embeddings()

        assert not [
            c for c in log.warning.call_args_list if c.args[0] == "embedding_unsearchable_backlog"
        ]

    @pytest.mark.asyncio
    async def test_a_failing_backlog_query_does_not_stop_the_sweep(self):
        """The count is a diagnostic. It must never be why embeddings stop."""
        ids = [uuid4()]
        mock_db = MagicMock()
        good = MagicMock()
        good.all.return_value = [(i,) for i in ids]
        mock_db.execute = AsyncMock(side_effect=[RuntimeError("boom"), good])

        with (
            patch("db.base.get_db", mock_get_db_factory(mock_db)),
            patch("services.memory_service.process_pending_embedding", new=AsyncMock()) as proc,
        ):
            await sweep_pending_embeddings()

        proc.assert_called_once()

    @pytest.mark.asyncio
    async def test_processes_pending_ids(self):
        """Each pending memory ID triggers process_pending_embedding."""
        ids = [uuid4(), uuid4()]
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [(i,) for i in ids]
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch("db.base.get_db", mock_get_db_factory(mock_db)),
            patch(
                "services.memory_service.process_pending_embedding", new=AsyncMock()
            ) as mock_process,
        ):
            await sweep_pending_embeddings()

        assert mock_process.call_count == 2
        mock_process.assert_any_call(ids[0])
        mock_process.assert_any_call(ids[1])

    @pytest.mark.asyncio
    async def test_limit_respected(self):
        """The query uses LIMIT 20."""
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        captured_stmt = None

        async def capture_execute(stmt):
            nonlocal captured_stmt
            captured_stmt = stmt
            return mock_result

        mock_db.execute.side_effect = capture_execute

        with patch("db.base.get_db", mock_get_db_factory(mock_db)):
            await sweep_pending_embeddings()

        assert captured_stmt is not None
        # Compiled SQL should contain LIMIT
        compiled = str(captured_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "LIMIT" in compiled.upper()

    @pytest.mark.asyncio
    async def test_select_includes_failed_retry_gate(self):
        """#979: the prefilter now also picks ``failed`` rows gated by the
        retry counter, without dropping the pending/processing branches.
        (The authoritative gate is re-checked in the claim — see
        tests/services/test_embedding_retry_claim.py.)"""
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        captured_stmt = None

        async def capture_execute(stmt):
            nonlocal captured_stmt
            captured_stmt = stmt
            return mock_result

        mock_db.execute = AsyncMock(side_effect=capture_execute)

        with patch("db.base.get_db", mock_get_db_factory(mock_db)):
            await sweep_pending_embeddings()

        from sqlalchemy.dialects import postgresql

        sql = str(
            captured_stmt.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert "'failed'" in sql
        assert "embedding_retry_count" in sql
        assert "'pending'" in sql  # existing branch preserved
        assert "'processing'" in sql  # existing branch preserved

    @pytest.mark.asyncio
    async def test_exception_during_sweep_is_logged(self):
        """Errors inside the sweep are caught and logged, not re-raised."""
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(side_effect=RuntimeError("DB exploded"))

        with patch("db.base.get_db", mock_get_db_factory(mock_db)):
            # Should not raise despite DB error
            await sweep_pending_embeddings()

    @pytest.mark.asyncio
    async def test_exception_during_single_embedding_is_logged(self):
        """One failing process_pending_embedding must not abort the rest.

        Asserts the iteration *actually* keeps going past the first
        failure: if the loop short-circuits on exception, ``call_count``
        would be 1 (only the failed first id ever attempted) and the
        test would silently pass without this assertion. Pin
        ``call_count == len(ids)`` and explicit verification that the
        second id was attempted, so a regression that aborts the sweep
        on first error fails loud.
        """
        ids = [uuid4(), uuid4()]
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [(i,) for i in ids]
        mock_db.execute = AsyncMock(return_value=mock_result)

        flaky_process = AsyncMock()

        async def flaky_side_effect(mid):
            if mid == ids[0]:
                raise RuntimeError("embedding failed")

        flaky_process.side_effect = flaky_side_effect

        with (
            patch("db.base.get_db", mock_get_db_factory(mock_db)),
            patch("services.memory_service.process_pending_embedding", flaky_process),
        ):
            await sweep_pending_embeddings()

        # Both should have been attempted (the second continues despite
        # first failure). Without these asserts the test silently passes
        # even if the sweep aborts on first exception.
        assert flaky_process.call_count == len(ids), (
            f"Expected {len(ids)} attempts (one per id) but only "
            f"{flaky_process.call_count} happened — sweep aborted early."
        )
        attempted_ids = {call.args[0] for call in flaky_process.call_args_list}
        assert ids[1] in attempted_ids, (
            "Second id was not attempted after first id raised — sweep "
            "failed to recover and continue iteration."
        )
