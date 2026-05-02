"""Tests for tasks/embedding_tasks.py.

Covers sweep logic and scheduler registration.  All heavy dependencies
(DB, embedding service) are patched so tests run without Docker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from tasks.embedding_tasks import schedule_embedding_tasks, sweep_pending_embeddings


def _mock_get_db(mock_db):
    """Return an async generator that yields mock_db once."""

    async def get_db():
        yield mock_db

    return get_db


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
        """When query returns nothing, function returns after the empty check."""
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("db.base.get_db", _mock_get_db(mock_db)):
            await sweep_pending_embeddings()

        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_processes_pending_ids(self):
        """Each pending memory ID triggers process_pending_embedding."""
        ids = [uuid4(), uuid4()]
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.all.return_value = [(i,) for i in ids]
        mock_db.execute = AsyncMock(return_value=mock_result)

        with (
            patch("db.base.get_db", _mock_get_db(mock_db)),
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

        with patch("db.base.get_db", _mock_get_db(mock_db)):
            await sweep_pending_embeddings()

        assert captured_stmt is not None
        # Compiled SQL should contain LIMIT
        compiled = str(captured_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "LIMIT" in compiled.upper()

    @pytest.mark.asyncio
    async def test_exception_during_sweep_is_logged(self):
        """Errors inside the sweep are caught and logged, not re-raised."""
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(side_effect=RuntimeError("DB exploded"))

        with patch("db.base.get_db", _mock_get_db(mock_db)):
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
            patch("db.base.get_db", _mock_get_db(mock_db)),
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
