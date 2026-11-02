"""#2476: the drain of the points forget could not delete after its commit."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services import deferred_point_deletes as deferred
from utils.exceptions import QdrantError


class FakeDb:
    """Records the drain's steps in order; answers its SELECTs."""

    def __init__(self, rows=(), owned=(), pending=None, oldest=12.5):
        self.rows = [
            SimpleNamespace(
                id=uuid4(),
                memory_id=memory_id,
                point_id=point_id,
                collection=collection,
                queued_at=MagicMock(name=f"queued_at-{i}"),
            )
            for i, (memory_id, point_id, collection) in enumerate(rows)
        ]
        self.owned = list(owned)
        self.pending = len(self.rows) if pending is None else pending
        self.oldest = oldest if self.pending else None
        self.steps: list[str] = []
        self.statements: dict[str, object] = {}
        self.execute = AsyncMock(side_effect=self._execute)
        self.commit = AsyncMock(side_effect=lambda: self.steps.append("commit"))
        self.rollback = AsyncMock(side_effect=lambda: self.steps.append("rollback"))

    async def _execute(self, stmt, *args, **kwargs):
        sql = str(stmt)
        if sql.startswith("DELETE"):
            step = "dequeue"
            result = MagicMock()
        elif sql.startswith("UPDATE"):
            step = "attempts"
            result = MagicMock()
        elif "count(*)" in sql:
            step = "backlog"
            result = MagicMock(one=MagicMock(return_value=(self.pending, self.oldest)))
        elif "FROM deferred_point_deletes" in sql:
            step = "rows"
            result = MagicMock(all=MagicMock(return_value=self.rows))
        else:
            step = "look"
            result = MagicMock(scalars=MagicMock(return_value=self.owned))
        self.steps.append(step)
        self.statements[step] = stmt
        return result


@pytest.fixture
def qdrant(request):
    calls: list = []

    async def points(ids, collection):
        calls.append(("points", collection, tuple(ids)))

    async def chunks(ids, collection):
        calls.append(("chunks", collection, tuple(ids)))

    with (
        patch("db.qdrant.delete_points_from_qdrant", AsyncMock(side_effect=points)) as p,
        patch("db.qdrant.delete_chunk_points", AsyncMock(side_effect=chunks)),
        patch(
            "db.qdrant.list_memory_collections", AsyncMock(return_value=["a", "b", "c", "c1"])
        ) as listed,
    ):
        p.calls = calls
        p.listed = listed
        yield p


def _lock(db: FakeDb, *, free: bool = True):
    async def lock(session, **kwargs):
        db.steps.append("lock")
        return free

    return lock


class TestDrain:
    @pytest.mark.asyncio
    async def test_takes_the_lock_before_reading_the_rows_and_deletes_them_after(self, qdrant):
        entry = (uuid4(), uuid4(), "c1")
        db = FakeDb(rows=[entry])
        with patch("db.point_writer_lock.wait_for_point_writers", _lock(db)):
            result = await deferred.drain_deferred_point_deletes(db, wait_seconds=30)

        assert db.steps == ["backlog", "rollback", "lock", "rows", "look", "dequeue", "commit"]
        assert qdrant.calls == [
            ("points", "c1", (str(entry[1]),)),
            ("chunks", "c1", (entry[0],)),
        ]
        assert (result.drained, result.kept, result.pending, result.busy) == (1, 0, 0, False)
        assert result.oldest_age_seconds == 12.5
        params = db.statements["dequeue"].compile().params["param_1"]
        assert params == [(db.rows[0].id, db.rows[0].queued_at)]

    @pytest.mark.asyncio
    async def test_the_rows_are_read_without_a_row_lock_oldest_first(self, qdrant):
        db = FakeDb(rows=[(uuid4(), uuid4(), "c")])
        with patch("db.point_writer_lock.wait_for_point_writers", _lock(db)):
            await deferred.drain_deferred_point_deletes(db, wait_seconds=30, limit=7)

        sql = str(db.statements["rows"])
        assert "FOR UPDATE" not in sql
        assert "ORDER BY deferred_point_deletes.queued_at, deferred_point_deletes.id" in sql
        assert db.statements["rows"].compile().params["param_1"] == 7

    @pytest.mark.asyncio
    async def test_groups_by_collection_points_before_chunks(self, qdrant):
        a1, a2, b1 = (uuid4(), uuid4(), "a"), (uuid4(), uuid4(), "a"), (uuid4(), uuid4(), "b")
        db = FakeDb(rows=[a1, b1, a2])
        with patch("db.point_writer_lock.wait_for_point_writers", _lock(db)):
            await deferred.drain_deferred_point_deletes(db, wait_seconds=30)

        assert qdrant.calls == [
            ("points", "a", (str(a1[1]), str(a2[1]))),
            ("chunks", "a", (a1[0], a2[0])),
            ("points", "b", (str(b1[1]),)),
            ("chunks", "b", (b1[0],)),
        ]

    @pytest.mark.asyncio
    async def test_a_live_owner_keeps_its_point_and_loses_its_row(self, qdrant):
        kept, freed = (uuid4(), uuid4(), "c"), (uuid4(), uuid4(), "c")
        db = FakeDb(rows=[kept, freed], owned=[kept[1]])
        with patch("db.point_writer_lock.wait_for_point_writers", _lock(db)):
            result = await deferred.drain_deferred_point_deletes(db, wait_seconds=30)

        assert qdrant.calls[0] == ("points", "c", (str(freed[1]),))
        assert (result.drained, result.kept) == (1, 1)
        dequeued = {pair[0] for pair in db.statements["dequeue"].compile().params["param_1"]}
        assert dequeued == {row.id for row in db.rows}

    @pytest.mark.asyncio
    async def test_writers_past_the_wait_leave_everything_queued(self, qdrant):
        db = FakeDb(rows=[(uuid4(), uuid4(), "c")])
        with patch("db.point_writer_lock.wait_for_point_writers", _lock(db, free=False)):
            result = await deferred.drain_deferred_point_deletes(db, wait_seconds=30)

        assert db.steps == ["backlog", "rollback", "lock", "rollback"]
        assert qdrant.calls == []
        assert result.busy is True
        assert result.pending == 1

    @pytest.mark.asyncio
    async def test_a_vector_store_failure_counts_an_attempt_and_does_not_raise(self, qdrant):
        db = FakeDb(rows=[(uuid4(), uuid4(), "c"), (uuid4(), uuid4(), "c")])
        qdrant.side_effect = QdrantError("down")
        with patch("db.point_writer_lock.wait_for_point_writers", _lock(db)):
            result = await deferred.drain_deferred_point_deletes(db, wait_seconds=30)

        # The commit releases the lock before the bump.
        assert db.steps == [
            "backlog",
            "rollback",
            "lock",
            "rows",
            "look",
            "commit",
            "attempts",
            "commit",
        ]
        update = db.statements["attempts"]
        assert "attempts=(deferred_point_deletes.attempts +" in str(update)
        assert set(update.compile().params["id_1"]) == {row.id for row in db.rows}
        assert result.failed == "QdrantError"
        assert (result.drained, result.pending) == (0, 2)

    @pytest.mark.asyncio
    async def test_one_failing_collection_does_not_hold_back_the_others(self, qdrant):
        bad, good = (uuid4(), uuid4(), "a"), (uuid4(), uuid4(), "b")
        db = FakeDb(rows=[bad, good])

        async def points(ids, collection):
            if collection == "a":
                raise QdrantError("a is read-only")

        qdrant.side_effect = points
        with patch("db.point_writer_lock.wait_for_point_writers", _lock(db)):
            result = await deferred.drain_deferred_point_deletes(db, wait_seconds=30)

        dequeued = {pair[0] for pair in db.statements["dequeue"].compile().params["param_1"]}
        assert dequeued == {db.rows[1].id}
        assert set(db.statements["attempts"].compile().params["id_1"]) == {db.rows[0].id}
        assert (result.drained, result.pending, result.failed) == (1, 1, "QdrantError")

    @pytest.mark.asyncio
    async def test_a_collection_that_is_gone_holds_no_point(self, qdrant):
        """A context migrated with --purge-source: nothing to delete, the row goes."""
        gone = (uuid4(), uuid4(), "kagura_memories_dropped")
        db = FakeDb(rows=[gone])
        with patch("db.point_writer_lock.wait_for_point_writers", _lock(db)):
            result = await deferred.drain_deferred_point_deletes(db, wait_seconds=30)

        assert qdrant.calls == []
        assert "dequeue" in db.steps
        assert "attempts" not in db.steps
        assert (result.pending, result.failed) == (0, None)

    @pytest.mark.asyncio
    async def test_unlisted_collections_take_no_lock(self, qdrant):
        db = FakeDb(rows=[(uuid4(), uuid4(), "c")])
        qdrant.listed.side_effect = QdrantError("down")
        lock = AsyncMock()
        with patch("db.point_writer_lock.wait_for_point_writers", lock):
            result = await deferred.drain_deferred_point_deletes(db, wait_seconds=30)

        lock.assert_not_awaited()
        assert db.steps == ["backlog", "rollback"]
        assert (result.failed, result.pending) == ("QdrantError", 1)

    @pytest.mark.asyncio
    async def test_an_empty_queue_takes_no_lock(self, qdrant):
        db = FakeDb()
        lock = AsyncMock()
        with (
            patch("db.point_writer_lock.wait_for_point_writers", lock),
            patch("db.point_writer_lock.try_exclude_point_writers", lock),
        ):
            result = await deferred.drain_deferred_point_deletes(db, wait_seconds=30)

        lock.assert_not_awaited()
        assert db.steps == ["backlog", "rollback"]
        assert (result.pending, result.oldest_age_seconds) == (0, None)

    @pytest.mark.asyncio
    async def test_the_wait_is_the_knob_and_zero_does_not_wait(self, qdrant):
        db = FakeDb(rows=[(uuid4(), uuid4(), "c")])
        wait = AsyncMock(return_value=True)
        try_lock = AsyncMock(return_value=True)
        with (
            patch("db.point_writer_lock.wait_for_point_writers", wait),
            patch("db.point_writer_lock.try_exclude_point_writers", try_lock),
        ):
            await deferred.drain_deferred_point_deletes(db, wait_seconds=4.5)
            wait.assert_awaited_once_with(db, timeout_seconds=4.5)
            try_lock.assert_not_awaited()

            await deferred.drain_deferred_point_deletes(db, wait_seconds=0)
            try_lock.assert_awaited_once_with(db)
            assert wait.await_count == 1


class TestQueuePointDeletes:
    @pytest.mark.asyncio
    async def test_nothing_to_queue_runs_no_statement(self):
        db = MagicMock(execute=AsyncMock())
        await deferred.queue_point_deletes(db, [])
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_entries_are_one_row(self):
        """ON CONFLICT DO UPDATE cannot touch one row twice in a statement."""
        entry = (uuid4(), uuid4(), "c")
        db = MagicMock(execute=AsyncMock())
        await deferred.queue_point_deletes(db, [entry, entry])
        params = db.execute.await_args.args[0].compile().params
        assert "memory_id_m0" in params
        assert "memory_id_m1" not in params

    @pytest.mark.asyncio
    async def test_a_requeued_key_gets_a_new_version(self):
        db = MagicMock(execute=AsyncMock())
        await deferred.queue_point_deletes(db, [(uuid4(), uuid4(), "c")])
        sql = str(db.execute.await_args.args[0])
        assert "DO UPDATE SET queued_at = clock_timestamp(), attempts = " in sql


class TestPointWriterWait:
    @pytest.mark.asyncio
    async def test_a_sub_millisecond_wait_is_never_lock_timeout_zero(self):
        """lock_timeout 0 is no timeout: the bounded wait would become unbounded."""
        from db.point_writer_lock import wait_for_point_writers

        db = MagicMock(execute=AsyncMock())
        await wait_for_point_writers(db, timeout_seconds=0.0001)
        assert db.execute.await_args_list[0].args[1] == {"timeout": "1ms"}
