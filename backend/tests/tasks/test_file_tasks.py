"""Tests for the orphan file sweeper (Issue #485 R3).

The sweeper logic is async + DB + Redis + R2, so the test mocks all
three boundaries: ``get_db`` is patched to yield a controlled
``AsyncSession`` mock, ``storage_quota_service.release_storage_bytes``
is patched, and ``get_blob_storage`` returns an in-memory fake. This
keeps the test purely a unit test that exercises the sweeper's
selection + per-row decision logic.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from tasks import file_tasks
from utils.datetime import utcnow


def _make_file(*, expires_at, status="reserved", size_bytes=1024, storage_key="ws/aa/key"):
    file = MagicMock()
    file.id = uuid4()
    file.workspace_id = uuid4()
    file.size_bytes = size_bytes
    file.storage_key = storage_key
    file.status = status
    file.expires_at = expires_at
    return file


def _patch_get_db(rows):
    """``async for db in get_db()`` yields one mock that returns ``rows``."""
    db = MagicMock()
    db.commit = AsyncMock()

    list_result = MagicMock()
    list_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=rows)))
    db.execute = AsyncMock(return_value=list_result)

    async def _aiter():
        yield db

    return patch("tasks.file_tasks.get_db", return_value=_aiter()), db


def _patch_release():
    return patch.object(file_tasks.storage_quota_service, "release_storage_bytes", AsyncMock())


def _patch_storage(storage_or_none=None):
    """Patch ``get_blob_storage`` to return ``storage_or_none``.

    ``None`` means the storage layer is not configured (dev/test).
    """
    if storage_or_none is None:
        return patch(
            "tasks.file_tasks.get_blob_storage",
            side_effect=RuntimeError("not configured"),
        )
    return patch("tasks.file_tasks.get_blob_storage", return_value=storage_or_none)


class TestSweepOrphanFiles:
    @pytest.mark.asyncio
    async def test_no_orphans_is_noop(self):
        rows: list = []
        get_db_patch, db = _patch_get_db(rows)
        with get_db_patch, _patch_release(), _patch_storage(None):
            counts = await file_tasks.sweep_orphan_files()
        assert counts == {"swept": 0, "released_bytes": 0, "r2_deleted": 0, "r2_failed": 0}
        db.commit.assert_awaited_once()

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
        db.commit.assert_awaited_once()

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


class TestScheduling:
    def test_schedule_registers_15min_interval_job(self):
        scheduler = MagicMock()
        scheduler.add_job = MagicMock()
        file_tasks.schedule_file_tasks(scheduler)

        scheduler.add_job.assert_called_once()
        kwargs = scheduler.add_job.call_args.kwargs
        assert kwargs["id"] == "orphan_file_sweeper"
        # Trigger interval should be 15 minutes
        trigger = kwargs["trigger"]
        # IntervalTrigger keeps the interval as a timedelta on `.interval`.
        from datetime import timedelta

        assert trigger.interval == timedelta(minutes=15)
