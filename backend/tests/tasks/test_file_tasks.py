"""Tests for the orphan file sweeper (Issue #485 R3).

The sweeper logic is async + DB + Redis + R2, so the test mocks all
three boundaries: ``get_db`` is patched to yield a controlled
``AsyncSession`` mock, ``storage_quota_service.release_storage_bytes``
is patched, and ``get_blob_storage`` returns an in-memory fake. This
keeps the test purely a unit test that exercises the sweeper's
selection + per-row decision logic.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from tasks import file_tasks
from utils.datetime import utcnow


def _make_file(
    *,
    expires_at=None,
    deleted_at=None,
    status="reserved",
    size_bytes=1024,
    storage_key="ws/aa/key",
):
    file = MagicMock()
    file.id = uuid4()
    file.workspace_id = uuid4()
    file.size_bytes = size_bytes
    file.storage_key = storage_key
    file.status = status
    file.expires_at = expires_at
    file.deleted_at = deleted_at
    return file


def _patch_get_db(rows, *, delete_rowcount: int = 1, stamp_rowcount: int = 0):
    """``async for db in get_db()`` yields one mock that returns ``rows``.

    The mock ``db.execute`` is **statement-type aware** (robust to call
    ordering): a ``Select`` returns the candidate ``rows``; a ``Delete``
    (per-row hard-delete in the GC sweep) returns ``delete_rowcount``; an
    ``Update`` (#962 orphan-sweep failed-row soft-delete stamp) returns
    ``stamp_rowcount``. Tests simulating a lost race pass
    ``delete_rowcount=0``; tests exercising the failed-row stamp pass
    ``stamp_rowcount=N``.
    """
    from sqlalchemy import Delete, Select, Update

    db = MagicMock()
    db.commit = AsyncMock()
    db.delete = AsyncMock()  # legacy compat: orphan sweeper does not use it

    list_result = MagicMock()
    list_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))

    delete_result = MagicMock()
    delete_result.rowcount = delete_rowcount

    stamp_result = MagicMock()
    stamp_result.rowcount = stamp_rowcount

    async def _execute(stmt, *_args, **_kwargs):
        if isinstance(stmt, Select):
            return list_result
        if isinstance(stmt, Update):
            return stamp_result
        if isinstance(stmt, Delete):
            return delete_result
        return delete_result

    db.execute = AsyncMock(side_effect=_execute)

    async def _aiter():
        yield db

    return patch("tasks.file_tasks.get_db", return_value=_aiter()), db


def _patch_release():
    return patch.object(file_tasks.storage_quota_service, "release_storage_bytes", AsyncMock())


def _patch_storage(storage_or_none=None):
    """Patch ``get_blob_storage`` to return ``storage_or_none``.

    ``None`` means the storage layer is not configured (dev/test).
    Factory now raises ``ExternalServiceError`` (HTTP 502 surface)
    instead of ``RuntimeError`` since Copilot loop 3 fix on PR #551.
    """
    if storage_or_none is None:
        from utils.exceptions import ExternalServiceError

        return patch(
            "tasks.file_tasks.get_blob_storage",
            side_effect=ExternalServiceError("R2", "not configured"),
        )
    return patch("tasks.file_tasks.get_blob_storage", return_value=storage_or_none)


class TestSweepOrphanFiles:
    @pytest.mark.asyncio
    async def test_no_orphans_is_noop(self):
        rows: list = []
        get_db_patch, db = _patch_get_db(rows)
        with get_db_patch, _patch_release(), _patch_storage(None):
            counts = await file_tasks.sweep_orphan_files()
        assert counts == {
            "swept": 0,
            "released_bytes": 0,
            "r2_deleted": 0,
            "r2_failed": 0,
            "failed_soft_deleted": 0,
        }
        # Two commits: the orphan-reap commit + the #962 failed-row stamp commit.
        assert db.commit.await_count == 2

    @pytest.mark.asyncio
    async def test_skips_rows_within_grace(self):
        """A reservation that just expired should NOT be swept until the
        1h grace has passed."""
        recent = _make_file(expires_at=utcnow() - timedelta(seconds=30))
        get_db_patch, _ = _patch_get_db([recent])
        with get_db_patch, _patch_release() as release, _patch_storage(None):
            counts = await file_tasks.sweep_orphan_files()
        assert counts["swept"] == 0
        release.assert_not_awaited()
        assert recent.status == "reserved"  # untouched

    @pytest.mark.asyncio
    async def test_sweeps_orphan_past_grace(self):
        old = _make_file(
            expires_at=utcnow() - timedelta(hours=2),  # past 1h grace
            size_bytes=4096,
        )
        get_db_patch, db = _patch_get_db([old])

        fake_storage = MagicMock()
        fake_storage.delete_object = AsyncMock()
        with get_db_patch, _patch_release() as release, _patch_storage(fake_storage):
            counts = await file_tasks.sweep_orphan_files()

        assert counts["swept"] == 1
        assert counts["released_bytes"] == 4096
        assert counts["r2_deleted"] == 1
        assert old.status == "failed"
        release.assert_awaited_once()
        fake_storage.delete_object.assert_awaited_once_with("ws/aa/key")
        # Orphan-reap commit + the #962 failed-row stamp commit.
        assert db.commit.await_count == 2

    @pytest.mark.asyncio
    async def test_r2_delete_failure_is_swallowed(self):
        """If R2 delete throws, the row is still marked failed and the
        Redis quota is still released — the binary becomes garbage and
        is reconciled later by lifecycle / inventory."""
        old = _make_file(expires_at=utcnow() - timedelta(hours=2))
        get_db_patch, _ = _patch_get_db([old])

        bad_storage = MagicMock()
        bad_storage.delete_object = AsyncMock(side_effect=Exception("R2 down"))
        with get_db_patch, _patch_release() as release, _patch_storage(bad_storage):
            counts = await file_tasks.sweep_orphan_files()

        assert counts["swept"] == 1
        assert counts["r2_deleted"] == 0
        assert counts["r2_failed"] == 1
        assert old.status == "failed"
        release.assert_awaited_once()  # quota still released

    @pytest.mark.asyncio
    async def test_skips_storage_when_r2_unconfigured(self):
        """In dev/test, R2 may be unconfigured. Sweep still marks rows
        failed and releases quota, just no R2 delete attempt."""
        old = _make_file(expires_at=utcnow() - timedelta(hours=2))
        get_db_patch, _ = _patch_get_db([old])

        with get_db_patch, _patch_release() as release, _patch_storage(None):
            counts = await file_tasks.sweep_orphan_files()

        assert counts["swept"] == 1
        assert counts["r2_deleted"] == 0
        assert counts["r2_failed"] == 0
        assert old.status == "failed"
        release.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stamps_lingering_failed_rows(self):
        """#962: ``failed AND deleted_at IS NULL`` rows (orphan-swept
        tracking rows, pre-#552 confirm_upload failures, Phase-1-era) get a
        ``deleted_at`` via the bulk stamp so the nightly GC can reap them.
        No reserved orphans here — only the stamp runs."""
        get_db_patch, db = _patch_get_db([], stamp_rowcount=3)
        with get_db_patch, _patch_release() as release, _patch_storage(None):
            counts = await file_tasks.sweep_orphan_files()
        assert counts["failed_soft_deleted"] == 3
        assert counts["swept"] == 0
        release.assert_not_awaited()  # stamp releases no quota (failed rows hold none)
        # Orphan-reap commit + the stamp commit.
        assert db.commit.await_count == 2

    @pytest.mark.asyncio
    async def test_stamp_issues_an_update(self):
        """The stamp is a single bulk ``UPDATE`` over failed+NULL rows."""
        from sqlalchemy import Update

        get_db_patch, db = _patch_get_db([], stamp_rowcount=1)
        with get_db_patch, _patch_release(), _patch_storage(None):
            await file_tasks.sweep_orphan_files()
        stmts = [c.args[0] for c in db.execute.await_args_list]
        assert any(isinstance(s, Update) for s in stmts), "expected a bulk UPDATE stamp"


class TestSweepSoftDeletedFiles:
    @pytest.mark.asyncio
    async def test_no_candidates_is_noop(self):
        rows: list = []
        get_db_patch, db = _patch_get_db(rows)
        fake_storage = MagicMock()
        fake_storage.delete_object = AsyncMock()
        with get_db_patch, _patch_storage(fake_storage):
            counts = await file_tasks.sweep_soft_deleted_files()
        assert counts == {"swept": 0, "r2_deleted": 0, "r2_failed": 0, "hard_deleted_no_r2": 0}
        db.delete.assert_not_awaited()
        fake_storage.delete_object.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_r2_unconfigured(self):
        """R2 unconfigured (dev/test) → early return, no DB ops at all.

        Hard-deleting a row whose binary we cannot clean up first would
        leak bytes forever — the safe move is to defer to the next
        sweep when R2 is configured."""
        # No _patch_get_db here: the function MUST short-circuit before
        # ever calling get_db.
        with _patch_storage(None):
            counts = await file_tasks.sweep_soft_deleted_files()
        assert counts == {"swept": 0, "r2_deleted": 0, "r2_failed": 0, "hard_deleted_no_r2": 0}

    @pytest.mark.asyncio
    async def test_hard_deletes_past_retention(self):
        """deleted_at older than 7d → R2 delete + idempotent SQL DELETE."""
        old = _make_file(
            status="uploaded",
            deleted_at=utcnow() - timedelta(days=8),
            size_bytes=4096,
        )
        get_db_patch, db = _patch_get_db([old])

        fake_storage = MagicMock()
        fake_storage.delete_object = AsyncMock()
        with get_db_patch, _patch_storage(fake_storage):
            counts = await file_tasks.sweep_soft_deleted_files()

        assert counts["swept"] == 1
        assert counts["r2_deleted"] == 1
        assert counts["r2_failed"] == 0
        fake_storage.delete_object.assert_awaited_once_with("ws/aa/key")
        # 2 db.execute calls: 1 SELECT + 1 DELETE (per-row).
        assert db.execute.await_count == 2
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gc_reaps_failed_status_rows(self):
        """#962: the GC now reaps ``status='failed'`` rows (stamped by the
        orphan sweeper), not just ``uploaded``. A failed row's NULL
        ``storage_key`` is the normal "reserved but never PUT" case → hard
        delete with no R2 op."""
        old = _make_file(
            status="failed",
            deleted_at=utcnow() - timedelta(days=8),
            storage_key=None,
        )
        get_db_patch, db = _patch_get_db([old])
        fake_storage = MagicMock()
        fake_storage.delete_object = AsyncMock()
        with get_db_patch, _patch_storage(fake_storage):
            counts = await file_tasks.sweep_soft_deleted_files()

        assert counts["swept"] == 1
        assert counts["hard_deleted_no_r2"] == 1
        assert counts["r2_deleted"] == 0
        fake_storage.delete_object.assert_not_awaited()
        # SELECT + per-row DELETE.
        assert db.execute.await_count == 2
        db.commit.assert_awaited_once()

        # Rigor: confirm the candidate SELECT actually widened to include
        # 'failed' (not merely that a failed row, once selected, is handled).
        select_stmt = db.execute.await_args_list[0].args[0]
        compiled = str(select_stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "'uploaded'" in compiled and "'failed'" in compiled

    @pytest.mark.asyncio
    async def test_lost_race_with_other_replica_is_no_op(self):
        """Multi-replica scenario: another sweeper already deleted the
        row. ``DELETE … WHERE id=… AND deleted_at IS NOT NULL`` returns
        ``rowcount=0`` and we must NOT increment ``swept`` or raise.

        Pre-#552 Copilot loop 1 fix the code used ``db.delete(orm)``
        which raises ``StaleDataError`` on a stale instance and
        aborted the entire batch."""
        old = _make_file(
            status="uploaded",
            deleted_at=utcnow() - timedelta(days=8),
            size_bytes=4096,
        )
        get_db_patch, db = _patch_get_db([old], delete_rowcount=0)

        fake_storage = MagicMock()
        fake_storage.delete_object = AsyncMock()
        with get_db_patch, _patch_storage(fake_storage):
            counts = await file_tasks.sweep_soft_deleted_files()

        # R2 delete still ran (the loop didn't know about the race
        # until DB returned rowcount=0). R2 DELETE is idempotent, so
        # this is a no-op on R2's side too.
        assert counts["r2_deleted"] == 1
        # Critical: ``swept`` stays 0 — we did NOT actually hard-delete
        # the row (another replica beat us).
        assert counts["swept"] == 0
        assert counts["r2_failed"] == 0

    @pytest.mark.asyncio
    async def test_r2_failure_leaves_row_for_next_sweep(self):
        """R2 delete throws → row NOT hard-deleted, ``r2_failed`` incremented."""
        old = _make_file(
            status="uploaded",
            deleted_at=utcnow() - timedelta(days=8),
        )
        get_db_patch, db = _patch_get_db([old])

        bad_storage = MagicMock()
        bad_storage.delete_object = AsyncMock(side_effect=Exception("R2 down"))
        with get_db_patch, _patch_storage(bad_storage):
            counts = await file_tasks.sweep_soft_deleted_files()

        assert counts["swept"] == 0
        assert counts["r2_deleted"] == 0
        assert counts["r2_failed"] == 1
        # Critical: only the SELECT runs; DELETE is NOT issued when
        # R2 failed — otherwise we'd lose the storage_key reference
        # and orphan the binary.
        assert db.execute.await_count == 1
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_r2_cancelled_error_propagates(self):
        """``asyncio.CancelledError`` from the R2 client during a sweep
        MUST propagate so APScheduler / lifespan shutdown can cancel
        the in-flight sweep cleanly. Pre-#552 Copilot loop 2 fix the
        broad ``except Exception`` swallowed cancellation."""
        old = _make_file(
            status="uploaded",
            deleted_at=utcnow() - timedelta(days=8),
        )
        get_db_patch, _ = _patch_get_db([old])

        cancel_storage = MagicMock()
        cancel_storage.delete_object = AsyncMock(side_effect=asyncio.CancelledError())
        with get_db_patch, _patch_storage(cancel_storage):
            with pytest.raises(asyncio.CancelledError):
                await file_tasks.sweep_soft_deleted_files()

    @pytest.mark.asyncio
    async def test_does_not_call_release_storage_bytes(self):
        """Quota was already released at soft-delete time (R5).

        Calling ``release_storage_bytes`` from the GC would
        double-decrement the workspace counter. Defense-in-depth:
        verify the GC path never even imports the release helper for
        these rows."""
        old = _make_file(
            status="uploaded",
            deleted_at=utcnow() - timedelta(days=8),
        )
        get_db_patch, _ = _patch_get_db([old])

        fake_storage = MagicMock()
        fake_storage.delete_object = AsyncMock()
        with get_db_patch, _patch_release() as release, _patch_storage(fake_storage):
            await file_tasks.sweep_soft_deleted_files()
        release.assert_not_awaited()


class TestScheduling:
    def test_schedule_registers_both_jobs(self):
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = MagicMock()
        scheduler.add_job = MagicMock()
        file_tasks.schedule_file_tasks(scheduler)

        # Two add_job calls: orphan sweeper + soft-delete GC.
        assert scheduler.add_job.call_count == 2

        calls_by_id = {call.kwargs["id"]: call.kwargs for call in scheduler.add_job.call_args_list}

        # Orphan sweeper retains the 15-minute interval.
        orphan = calls_by_id["orphan_file_sweeper"]
        assert isinstance(orphan["trigger"], IntervalTrigger)
        assert orphan["trigger"].interval == timedelta(minutes=15)

        # Soft-delete GC fires nightly. ``str(CronTrigger)`` renders as
        # ``cron[hour='3', minute='15']`` in APScheduler 3.x — checking
        # the rendered form avoids reaching into trigger internals.
        gc = calls_by_id["soft_delete_file_gc"]
        assert isinstance(gc["trigger"], CronTrigger)
        gc_repr = str(gc["trigger"])
        assert "hour='3'" in gc_repr
        assert "minute='15'" in gc_repr
