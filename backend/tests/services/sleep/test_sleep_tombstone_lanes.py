"""#1520: consolidation archives are tombstones in the SAME retention lane as
merge losers (``sleep_merge_retention_days``), and never in the forget lane.

The lane predicates are derived from one set (``SLEEP_TOMBSTONE_DELETED_BY``)
so adding a sleep tombstone class cannot silently land in the user-forget
window — the forget lane used to be defined as "everything that is not the
merge sentinel", which is exactly the shape that breaks when a value is added.
"""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy import select

from models.memory import Memory
from repositories.memory import MemoryRepository
from services.sleep.merge_retention import (
    MergeRetentionPhase,
    restore_sleep_tombstone_stmt,
    sleep_tombstone_predicate,
    user_tombstone_predicate,
)
from utils.datetime import utcnow


def _config(days: int) -> MagicMock:
    cfg = MagicMock()
    cfg.sleep_merge_retention_days = days
    return cfg


def _budget() -> MagicMock:
    budget = MagicMock()
    budget.exhausted = False
    return budget


def _tombstone(user: str, deleted_by: str | None, *, age_days: int) -> Memory:
    return Memory(
        id=uuid4(),
        user_id=user,
        summary=f"tombstone {deleted_by}",
        content="c",
        type="note",
        client="pytest",
        scope="working",
        deleted_at=utcnow() - timedelta(days=age_days),
        deleted_by=deleted_by,
    )


class TestMergeLaneRealDB:
    async def test_archive_tombstones_share_the_merge_retention_window(self, db_session):
        user = f"lane-user-{uuid4()}"
        merge_loser = _tombstone(user, "sleep_maintenance", age_days=10)
        archived = _tombstone(user, "sleep_consolidation", age_days=10)
        forgotten = _tombstone(user, "some-user-sub", age_days=10)
        db_session.add_all([merge_loser, archived, forgotten])
        await db_session.flush()

        result = await MergeRetentionPhase(db_session).execute(
            _config(7), user, None, None, _budget()
        )

        assert result.details["purged"] == 2
        remaining = (
            (await db_session.execute(select(Memory.id).where(Memory.user_id == user)))
            .scalars()
            .all()
        )
        assert remaining == [forgotten.id]


class TestLanePredicates:
    """The forget lane is the complement of the sleep-tombstone set, NULL included
    (legacy forget rows predate the column). Pinned as compiled SQL, following
    test_forget_retention's precedent for this predicate."""

    def test_sleep_lane_names_both_sentinels(self):
        sql = str(sleep_tombstone_predicate().compile(compile_kwargs={"literal_binds": True}))
        assert "sleep_maintenance" in sql
        assert "sleep_consolidation" in sql

    def test_restore_only_matches_the_named_sleep_tombstone_in_a_live_context(self):
        """#1520 review: rowcount == 1 must mean "a sleep tombstone of this class was
        restored" — never "some row for this user exists". A user forget (deleted_by
        = actor sub) and a row inside a deleted context must not match."""
        stmt = restore_sleep_tombstone_stmt(uuid4(), "u", deleted_by="sleep_consolidation")
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "memories.deleted_at IS NOT NULL" in sql
        assert "memories.deleted_by = 'sleep_consolidation'" in sql
        assert "contexts.deleted_at IS NULL" in sql

    def test_user_lane_excludes_both_sentinels_and_keeps_null(self):
        sql = str(user_tombstone_predicate().compile(compile_kwargs={"literal_binds": True}))
        assert "sleep_maintenance" in sql
        assert "sleep_consolidation" in sql
        assert "NOT IN" in sql
        assert "IS NULL" in sql


class TestArchiveRollbackRealDB:
    """#1520 end to end on Postgres: the archive tombstone is what rollback restores."""

    async def test_archive_then_rollback_restores_the_row(self, db_session):
        from mcp_server.tools.sleep import _RollbackCtx, _undo_archive

        user = f"rb-user-{uuid4()}"
        row = Memory(
            id=uuid4(),
            user_id=user,
            summary="archived then rolled back",
            content="c",
            type="note",
            client="pytest",
            scope="working",
        )
        db_session.add(row)
        await db_session.flush()
        assert (
            await MemoryRepository(db_session).soft_delete(row.id, deleted_by="sleep_consolidation")
            == 1
        )

        ctx = _RollbackCtx(
            user_id=user,
            ws_id="ws",
            ctx_id_str="ctx",
            collection_name="kagura_memories",
            embedding_svc=MagicMock(),
            memory_cache={row.id: row},
            edge_repo=MagicMock(),
        )
        action = MagicMock()
        action.memory_id = row.id
        action.details = {"mode": "tombstone"}
        summary = {"archives_restored": 0, "errors": []}
        with patch("mcp_server.tools.sleep._re_embed_to_qdrant", new_callable=AsyncMock) as reembed:
            await _undo_archive(db_session, action, ctx, summary)
        await db_session.refresh(row)
        assert summary == {"archives_restored": 1, "errors": []}
        assert row.deleted_at is None and row.deleted_by is None
        reembed.assert_awaited_once()

        # A row that is no longer there (older hard-delete, or purged) is an error, not a restore.
        gone = MagicMock()
        gone.memory_id = uuid4()
        gone.details = {"mode": "tombstone"}
        with patch("mcp_server.tools.sleep._re_embed_to_qdrant", new_callable=AsyncMock):
            await _undo_archive(db_session, gone, ctx, summary)
        assert summary["archives_restored"] == 1
        assert len(summary["errors"]) == 1 and str(gone.memory_id) in summary["errors"][0]

        # A user's own forget() tombstone under an archive action (the fetch/stamp
        # race) must NOT be resurrected by a sleep rollback.
        forgotten = Memory(
            id=uuid4(),
            user_id=user,
            summary="forgotten by the user",
            content="c",
            type="note",
            client="pytest",
            scope="working",
            deleted_at=utcnow(),
            deleted_by="some-user-sub",
        )
        db_session.add(forgotten)
        await db_session.flush()
        ctx.memory_cache[forgotten.id] = forgotten
        stale = MagicMock()
        stale.memory_id = forgotten.id
        stale.details = {"mode": "tombstone"}
        with patch("mcp_server.tools.sleep._re_embed_to_qdrant", new_callable=AsyncMock) as reembed:
            await _undo_archive(db_session, stale, ctx, summary)
        await db_session.refresh(forgotten)
        assert forgotten.deleted_at is not None and forgotten.deleted_by == "some-user-sub"
        assert summary["archives_restored"] == 1
        assert len(summary["errors"]) == 2
        reembed.assert_not_awaited()
