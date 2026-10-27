"""Tests for tasks/embedding_tasks.py.

Covers sweep logic and scheduler registration.  All heavy dependencies
(DB, embedding service) are patched so tests run without Docker.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from tasks.embedding_tasks import (
    reconcile_content_chunk_gaps,
    schedule_embedding_tasks,
    sweep_pending_embeddings,
)
from tests.tasks.conftest import mock_get_db_factory


class TestScheduleEmbeddingTasks:
    def test_registers_job_with_correct_interval(self):
        scheduler = MagicMock()
        schedule_embedding_tasks(scheduler)

        assert scheduler.add_job.call_count == 2
        call = scheduler.add_job.call_args_list[0]
        assert call.kwargs["id"] == "sweep_pending_embeddings"
        assert call.kwargs["name"] == "Sweep Pending Embeddings"
        assert call.kwargs["replace_existing"] is True
        # IntervalTrigger(seconds=30)
        trigger = call.kwargs["trigger"]
        assert trigger.interval.total_seconds() == 30

    def test_registers_the_chunk_gap_reconciler(self):
        """#2107: the gap reconciler runs beside the sweep, every 60 s, no env knob."""
        scheduler = MagicMock()
        with capture_logs() as logs:
            schedule_embedding_tasks(scheduler)

        call = scheduler.add_job.call_args_list[1]
        assert call.args[0] is reconcile_content_chunk_gaps
        assert call.kwargs["id"] == "reconcile_content_chunk_gaps"
        assert call.kwargs["replace_existing"] is True
        assert call.kwargs["trigger"].interval.total_seconds() == 60
        assert ("scheduled_content_chunks_gap_task", "info") in _events(logs)


def _events(logs: list[dict]) -> list[tuple[str, str]]:
    return [(e["event"], e["log_level"]) for e in logs]


class TestReconcileContentChunkGapsTask:
    @pytest.fixture(autouse=True)
    def _fresh_seed(self, monkeypatch):
        from tasks import embedding_tasks

        monkeypatch.setattr(embedding_tasks._SeedState, "done", False)

    def _db(self):
        db = MagicMock()
        db.rollback = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_seeds_once_then_only_reconciles(self):
        """#2107: the one-time seed runs on the leader's first tick, then never again."""
        from services.content_chunk_gaps import GapReconcileResult

        db = self._db()
        seed = AsyncMock(return_value=None)
        tick = AsyncMock(return_value=GapReconcileResult(scanned=3, requeued=1))
        with (
            patch("db.base.get_db", mock_get_db_factory(db)),
            patch("services.content_chunk_gaps.seed_content_chunk_gaps", seed),
            patch("services.content_chunk_gaps.reconcile_content_chunk_gaps", tick),
            capture_logs() as logs,
        ):
            await reconcile_content_chunk_gaps()
            await reconcile_content_chunk_gaps()
        seed.assert_awaited_once_with(db)
        assert tick.await_count == 2
        assert _events(logs) == [("content_chunks_gap_reconcile_done", "info")] * 2

    @pytest.mark.asyncio
    async def test_a_failed_seed_rolls_back_and_is_retried_next_tick(self):
        from services.content_chunk_gaps import GapReconcileResult

        db = self._db()
        seed = AsyncMock(side_effect=[RuntimeError("lock timeout"), 2])
        tick = AsyncMock(return_value=GapReconcileResult())
        with (
            patch("db.base.get_db", mock_get_db_factory(db)),
            patch("services.content_chunk_gaps.seed_content_chunk_gaps", seed),
            patch("services.content_chunk_gaps.reconcile_content_chunk_gaps", tick),
            capture_logs() as logs,
        ):
            await reconcile_content_chunk_gaps()  # logged; the tick still runs
            await reconcile_content_chunk_gaps()
            await reconcile_content_chunk_gaps()
        assert seed.await_count == 2
        assert tick.await_count == 3
        db.rollback.assert_awaited_once()
        assert _events(logs) == [("content_chunks_gap_seed_failed", "error")]

    @pytest.mark.asyncio
    async def test_a_failed_tick_is_rolled_back_and_swallowed(self):
        db = self._db()
        with (
            patch("db.base.get_db", mock_get_db_factory(db)),
            patch("services.content_chunk_gaps.seed_content_chunk_gaps", AsyncMock()),
            patch(
                "services.content_chunk_gaps.reconcile_content_chunk_gaps",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
            capture_logs() as logs,
        ):
            await reconcile_content_chunk_gaps()  # logged, not raised
        db.rollback.assert_awaited_once()
        assert _events(logs) == [("content_chunks_gap_reconcile_tick_failed", "error")]


class TestSweepPendingEmbeddings:
    @pytest.fixture(autouse=True)
    def _no_exhaust_step(self, monkeypatch):
        """These tests script the backlog count and the candidate query; the
        #2095 exhaust step has its own tests below."""
        from tasks import embedding_tasks

        monkeypatch.setattr(embedding_tasks, "_fail_exhausted_reclaims", AsyncMock())

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
        mock_result.one.return_value = SimpleNamespace(
            unsearchable=467, stalled=467, capped=400, configuration=7, erased=20
        )
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch("db.base.get_db", mock_get_db_factory(mock_db)):
            with patch("tasks.embedding_tasks.logger") as log:
                await sweep_pending_embeddings()

        warnings = [
            c for c in log.warning.call_args_list if c.args[0] == "embedding_unsearchable_backlog"
        ]
        assert warnings, "the backlog was not reported"
        # #2015 D6: split by error class, so a capped workspace reads as capped.
        assert warnings[0].kwargs == {
            "unsearchable": 467,
            "stalled": 467,
            "capped": 400,
            "configuration": 7,
            # #2363: an erasure's dequeued survivors, not a provider problem
            "erased": 20,
            "other": 40,
        }
        backlog_sql = str(
            mock_db.execute.await_args_list[0]
            .args[0]
            .compile(compile_kwargs={"literal_binds": True})
        )
        assert "LIKE 'QUOTA-002:%'" in backlog_sql
        assert "LIKE 'CFG-%'" in backlog_sql and "LIKE 'FEAT-%'" in backlog_sql
        # an exact match on the erased-author marker, not a prefix
        from config.constants import ERASED_AUTHOR_EMBEDDING_ERROR

        marker = ERASED_AUTHOR_EMBEDDING_ERROR.replace("'", "''")
        assert f"memories.embedding_error = '{marker}'" in backlog_sql

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
        # #2095: the stale-processing branch is bounded by the retry budget
        # too (shared with the claim), so an exhausted row is not a candidate.
        processing = sql.split("embedding_status = 'processing'", 1)[1]
        assert "embedding_retry_count < 3" in processing.split("embedding_status = 'failed'")[0]

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


def _pg(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class TestFailExhaustedReclaims:
    """#2095: a stale processing row with no budget left is failed by the sweep."""

    def _db(self, *results):
        db = MagicMock()
        db.execute = AsyncMock(side_effect=list(results))
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_runs_before_the_backlog_count_and_the_candidate_query(self):
        """First, so the tick that fails a row also counts it as stalled."""
        mid = uuid4()
        backlog = MagicMock()
        backlog.one.return_value = SimpleNamespace(unsearchable=0, stalled=0)
        exhausted = MagicMock()
        exhausted.all.return_value = [(mid, 3)]
        candidates = MagicMock()
        candidates.all.return_value = []
        db = self._db(exhausted, backlog, candidates)

        with (
            patch("db.base.get_db", mock_get_db_factory(db)),
            capture_logs() as logs,
        ):
            await sweep_pending_embeddings()

        stmts = [c.args[0] for c in db.execute.await_args_list]
        assert len(stmts) == 3
        update_sql = _pg(stmts[0])
        assert "count(*)" in _pg(stmts[1])
        assert update_sql.startswith("UPDATE memories SET embedding_status='failed'")
        assert "claim_expired: the run outlived its claim on every retry" in update_sql
        where = update_sql.split(" WHERE ", 1)[1]
        assert "embedding_status = 'processing'" in where
        assert "embedding_retry_count >= 3" in where
        assert "deleted_at IS NULL" in where
        assert "embedding_attempted_at IS NULL" in where  # NULL-safe, as the claim
        assert "updated_at" not in update_sql.replace("embedding_attempted_at", "")  # #1852
        assert _pg(stmts[2]).startswith("SELECT")
        db.commit.assert_awaited_once()
        exhausted_logs = [e for e in logs if e["event"] == "embedding_budget_exhausted"]
        assert exhausted_logs == [
            {
                "event": "embedding_budget_exhausted",
                "log_level": "error",
                "memory_id": str(mid),
                "reason": "claim_expired",
                "retry_count": 3,
            }
        ]

    @pytest.mark.asyncio
    async def test_uses_the_claims_stale_cutoff(self):
        """Another clock could fail a live run that still holds its claim."""
        from datetime import datetime, timedelta

        from services.embedding_claim import STALE_CLAIM_SECONDS
        from tasks.embedding_tasks import _fail_exhausted_reclaims

        now = datetime(2026, 1, 2, 3, 4, 5)
        cutoff = now - timedelta(seconds=STALE_CLAIM_SECONDS)
        result = MagicMock()
        result.all.return_value = []
        db = self._db(result)

        await _fail_exhausted_reclaims(db, cutoff, now)

        sql = _pg(db.execute.await_args.args[0])
        assert f"embedding_attempted_at < '{cutoff.isoformat(sep=' ')}'" in sql

    @pytest.mark.asyncio
    async def test_a_failed_exhaust_update_does_not_stop_the_sweep(self):
        mid = uuid4()
        backlog = MagicMock()
        backlog.one.return_value = SimpleNamespace(unsearchable=0, stalled=0)
        candidates = MagicMock()
        candidates.all.return_value = [(mid,)]
        db = self._db(RuntimeError("lock timeout"), backlog, candidates)

        with (
            patch("db.base.get_db", mock_get_db_factory(db)),
            patch("services.memory_service.process_pending_embedding", new=AsyncMock()) as proc,
            capture_logs() as logs,
        ):
            await sweep_pending_embeddings()

        db.rollback.assert_awaited_once()
        proc.assert_awaited_once_with(mid)
        assert ("embedding_reclaim_exhaust_failed", "error") in _events(logs)
