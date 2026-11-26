"""Lock-ordered bulk access-stat and promotion writes (Issue #1985).

Concurrent recalls whose pages overlap used to bump ``access_count`` one row
at a time in ranking order, so two requests could lock the same rows in
opposite orders and deadlock. ``bulk_update_access_stats`` takes every row
lock in one statement, in ``id`` order, through an ordered ``FOR UPDATE``
subquery; ``bulk_promote_to_persistent`` does the same for promotion.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from models.memory import Memory
from repositories.memory import (
    ACCESS_STATS_STATEMENT,
    ADOPTION_ACCESS_STATS_STATEMENT,
    DELIVERY_STAMP_STATEMENT,
    PROMOTE_SKIP_LOCKED_STATEMENT,
    PROMOTE_STATEMENT,
    MemoryRepository,
)

_async = pytest.mark.asyncio(loop_scope="session")


def _memory(**overrides) -> Memory:
    values = {
        "workspace_id": uuid4(),
        "id": uuid4(),
        "user_id": "bulk_stats_user",
        "summary": "bulk stats",
        "content": "bulk stats content",
        "type": "note",
        "scope": "working",
        "client": "test",
        "created_at": datetime.utcnow(),
    }
    values.update(overrides)
    return Memory(**values)


class TestStatementShape:
    """The lock order lives in the SQL text, so pin it."""

    @pytest.mark.parametrize(
        ("statement", "count_as_adoption"),
        [(ACCESS_STATS_STATEMENT, False), (ADOPTION_ACCESS_STATS_STATEMENT, True)],
    )
    def test_access_stats_locks_rows_in_id_order(self, statement, count_as_adoption):
        sql = " ".join(str(statement).split())

        # #1985: ordered and never waiting — a held row is skipped.
        assert "ORDER BY id FOR UPDATE SKIP LOCKED" in sql
        assert "id = ANY(:ids)" in sql
        assert "deleted_at IS NULL" in sql
        assert "WHERE m.id = locked.id" in sql
        assert (
            "RETURNING m.id, m.access_count, m.reference_count, m.last_used_at, "
            "m.accessed_by_clients" in sql
        )
        assert ("reference_count = m.reference_count + 1" in sql) is count_as_adoption
        # #457: the client append is a CASE, never a WHERE filter, so a row the
        # client already touched still gets its access_count bump.
        assert "CASE WHEN" in sql and "array_append" in sql

    def test_delivery_stamp_rides_on_the_access_statement(self):
        """#3022: no extra statement for the main page; the surfacing write
        stamps only ``:delivered``, the adoption write every row, and the
        stamp-only statement (the user facts lane) moves no counter."""
        surfacing = " ".join(str(ACCESS_STATS_STATEMENT).split())
        adoption = " ".join(str(ADOPTION_ACCESS_STATS_STATEMENT).split())
        stamp = " ".join(str(DELIVERY_STAMP_STATEMENT).split())

        assert "last_delivered_at = CASE WHEN m.id = ANY(:delivered) THEN :now" in surfacing
        assert "COALESCE(m.first_delivered_at, :now)" in surfacing
        assert "last_delivered_at = :now" in adoption and ":delivered" not in adoption
        assert "first_delivered_at = COALESCE(m.first_delivered_at, :now)" in adoption
        assert "ORDER BY id FOR UPDATE SKIP LOCKED" in stamp
        assert "access_count" not in stamp and "last_used_at" not in stamp
        assert "first_delivered_at = COALESCE(m.first_delivered_at, :now)" in stamp

    def test_promotion_locks_rows_in_id_order(self):
        sql = " ".join(str(PROMOTE_STATEMENT).split())
        skip_sql = " ".join(str(PROMOTE_SKIP_LOCKED_STATEMENT).split())

        # Sleep's promotion waits; recall's skips a held row.
        assert "ORDER BY id FOR UPDATE)" in sql and "SKIP LOCKED" not in sql
        assert "ORDER BY id FOR UPDATE SKIP LOCKED)" in skip_sql
        assert skip_sql.replace(" SKIP LOCKED", "") == sql
        assert "scope <> 'persistent'" in sql
        assert "RETURNING m.id, m.promoted_at" in sql

    @_async
    async def test_empty_ids_run_no_query(self):
        db = MagicMock()
        db.execute = AsyncMock()
        repo = MemoryRepository(db)

        assert await repo.bulk_update_access_stats([], client="api") == {}
        assert await repo.bulk_promote_to_persistent([]) == []
        db.execute.assert_not_awaited()


@_async
class TestBulkAccessStats:
    async def test_bumps_every_row_once_and_appends_client_once(self, db_session):
        first, second = _memory(), _memory(accessed_by_clients=["claude"])
        db_session.add_all([first, second])
        await db_session.commit()
        repo = MemoryRepository(db_session)

        await repo.bulk_update_access_stats([second.id, first.id], client="api")
        counts = await repo.bulk_update_access_stats([first.id, second.id], client="api")
        await db_session.commit()

        assert {k: v.access_count for k, v in counts.items()} == {first.id: 2, second.id: 2}
        assert counts[second.id].accessed_by_clients == ["claude", "api"]
        assert counts[first.id].scope == "working"
        await db_session.refresh(first)
        await db_session.refresh(second)
        assert first.accessed_by_clients == ["api"]
        assert second.accessed_by_clients == ["claude", "api"]
        assert first.reference_count == 0
        assert first.last_used_at is not None

    async def test_adoption_bumps_reference_count(self, db_session):
        memory = _memory()
        db_session.add(memory)
        await db_session.commit()

        await MemoryRepository(db_session).bulk_update_access_stats(
            [memory.id], client="api", count_as_adoption=True
        )
        await db_session.commit()
        await db_session.refresh(memory)

        assert (memory.access_count, memory.reference_count) == (1, 1)

    async def test_skips_soft_deleted_and_unknown_rows(self, db_session):
        live, gone = _memory(), _memory(deleted_at=datetime.utcnow())
        db_session.add_all([live, gone])
        await db_session.commit()

        counts = await MemoryRepository(db_session).bulk_update_access_stats(
            [gone.id, uuid4(), live.id], client="api"
        )
        await db_session.commit()
        await db_session.refresh(gone)

        assert {k: v.access_count for k, v in counts.items()} == {live.id: 1}
        assert gone.access_count == 0

    async def test_does_not_touch_updated_at(self, db_session):
        memory = _memory()
        db_session.add(memory)
        await db_session.commit()
        await db_session.refresh(memory)
        before = memory.updated_at

        await MemoryRepository(db_session).bulk_update_access_stats([memory.id], client="api")
        await db_session.commit()
        await db_session.refresh(memory)

        assert memory.updated_at == before  # #1317


@_async
class TestDeliveryStamp:
    """#3022: when a memory's content was first and last shipped to a reader."""

    async def test_a_surfacing_bump_stamps_only_the_delivered_rows(self, db_session):
        shipped, summary_only = _memory(), _memory()
        db_session.add_all([shipped, summary_only])
        await db_session.commit()
        repo = MemoryRepository(db_session)

        stats = await repo.bulk_update_access_stats(
            [shipped.id, summary_only.id], client="api", delivered_ids=[shipped.id]
        )
        await db_session.commit()
        await db_session.refresh(shipped)
        await db_session.refresh(summary_only)

        assert shipped.last_delivered_at is not None
        assert shipped.first_delivered_at == shipped.last_delivered_at
        assert stats[shipped.id].last_delivered_at == shipped.last_delivered_at
        assert (summary_only.first_delivered_at, summary_only.last_delivered_at) == (None, None)
        assert summary_only.access_count == 1  # bumped, not stamped

    async def test_without_delivered_ids_nothing_is_stamped(self, db_session):
        """Explore and other surfacing reads ship no content."""
        memory = _memory()
        db_session.add(memory)
        await db_session.commit()

        await MemoryRepository(db_session).bulk_update_access_stats([memory.id], client="api")
        await db_session.commit()
        await db_session.refresh(memory)

        assert (memory.access_count, memory.last_delivered_at) == (1, None)

    async def test_first_delivery_is_kept_and_last_moves(self, db_session):
        first = datetime.utcnow() - timedelta(days=3)
        memory = _memory(first_delivered_at=first, last_delivered_at=first)
        db_session.add(memory)
        await db_session.commit()

        await MemoryRepository(db_session).bulk_update_access_stats(
            [memory.id], client="api", count_as_adoption=True
        )
        await db_session.commit()
        await db_session.refresh(memory)

        assert memory.first_delivered_at == first
        assert memory.last_delivered_at is not None and memory.last_delivered_at > first

    async def test_the_stamp_alone_moves_no_counter(self, db_session):
        memory, gone = _memory(), _memory(deleted_at=datetime.utcnow())
        db_session.add_all([memory, gone])
        await db_session.commit()

        stamped = await MemoryRepository(db_session).stamp_delivery([memory.id, gone.id, uuid4()])
        await db_session.commit()
        await db_session.refresh(memory)
        await db_session.refresh(gone)

        assert stamped == {memory.id}
        assert memory.last_delivered_at is not None
        assert memory.first_delivered_at == memory.last_delivered_at
        assert (memory.access_count, memory.reference_count, memory.last_used_at) == (0, 0, None)
        assert gone.last_delivered_at is None

    async def test_the_stamp_does_not_touch_updated_at(self, db_session):
        memory = _memory()
        db_session.add(memory)
        await db_session.commit()
        await db_session.refresh(memory)
        before = memory.updated_at

        await MemoryRepository(db_session).stamp_delivery([memory.id])
        await db_session.commit()
        await db_session.refresh(memory)

        assert memory.updated_at == before


@_async
class TestBulkPromote:
    async def test_promotes_only_rows_not_yet_persistent(self, db_session):
        working = _memory(created_at=datetime.utcnow() - timedelta(days=1))
        persistent = _memory(scope="persistent")
        db_session.add_all([working, persistent])
        await db_session.commit()

        promoted = await MemoryRepository(db_session).bulk_promote_to_persistent(
            [persistent.id, working.id]
        )
        await db_session.commit()
        await db_session.refresh(working)
        await db_session.refresh(persistent)

        assert promoted == [working.id]
        assert working.scope == "persistent"
        assert working.promoted_at is not None
        assert persistent.promoted_at is None


@_async
class TestIdentityMapStaysCurrent:
    """The set-based UPDATE bypasses the ORM, so a row already loaded in the
    session is brought up to date without being marked dirty."""

    async def test_loaded_row_shows_the_bump_without_a_pending_update(self, db_session):
        memory = _memory(accessed_by_clients=["claude"])
        db_session.add(memory)
        await db_session.commit()
        repo = MemoryRepository(db_session)

        await repo.bulk_update_access_stats([memory.id], client="api", count_as_adoption=True)

        assert memory.access_count == 1
        assert memory.reference_count == 1
        assert memory.last_used_at is not None
        assert memory.accessed_by_clients == ["claude", "api"]
        assert memory not in db_session.dirty
        await db_session.commit()

        # Nothing was re-written on flush: a second bump still counts from 1.
        await repo.bulk_update_access_stats([memory.id], client="api")
        await db_session.commit()
        await db_session.refresh(memory)
        assert (memory.access_count, memory.reference_count) == (2, 1)
        assert memory.accessed_by_clients == ["claude", "api"]

    async def test_loaded_row_shows_its_promotion(self, db_session):
        memory = _memory()
        db_session.add(memory)
        await db_session.commit()

        await MemoryRepository(db_session).bulk_promote_to_persistent([memory.id])

        assert memory.scope == "persistent"
        assert memory.promoted_at is not None
        assert memory not in db_session.dirty
        await db_session.commit()
