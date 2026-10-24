"""Run-level Sleep rollback against a real Postgres (#2065).

Each undo runs in a SAVEPOINT of its own, so an undo that raises leaves
nothing behind while the run's other undos still commit. Before, the final
commit also saved the failed undo's restore UPDATE: a row whose point could
not be rebuilt (a resource whose schema is gone) went live with no point,
under a report the rollback then refused to run again.

A restore whose point write fails on a ``success`` row re-queues the row
``pending`` instead (the embedding sweep rebuilds the point, through the
resource indexer for a resource row) and counts as restored. The vector
store is not needed: the point writer is faked. Skipped when Postgres is
unreachable.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import DELETED_BY_SLEEP_ARCHIVE, Memory
from models.sleep import SleepAction, SleepReport
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_USER = "sleep-rollback-savepoint-it"


def _archived(summary: str, *, status: str = "success", importance: float = 0.5) -> Memory:
    return Memory(
        id=uuid4(),
        workspace_id=uuid4(),
        user_id=_USER,
        summary=summary,
        content="c",
        type="note",
        scope="working",
        client="test",
        importance=importance,
        embedding_status=status,
        deleted_at=utcnow() - timedelta(days=1),
        deleted_by=DELETED_BY_SLEEP_ARCHIVE,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def session(db_session: AsyncSession) -> AsyncIterator[AsyncSession]:
    yield db_session
    await db_session.rollback()
    await db_session.execute(delete(Memory).where(Memory.user_id == _USER))
    await db_session.execute(delete(SleepReport).where(SleepReport.user_id == _USER))
    await db_session.commit()


async def test_a_failed_undo_is_rolled_back_alone(session: AsyncSession) -> None:
    from mcp_server.tools.sleep import handle_rollback_sleep_run

    # The point of ``unrebuildable`` cannot be written and its re-queue fails
    # too: its undo raises and must leave the row a tombstone.
    unrebuildable = _archived("point cannot be rebuilt", status="failed")
    # The point of ``requeued`` cannot be written either: restored and handed
    # to the embedding sweep, also as a ``failed`` row (#2065: the re-queue is
    # not fenced on ``success``; the point is gone whatever the status says).
    requeued = _archived("point re-queued", status="failed")
    restored = _archived("point rebuilt")
    rescored = Memory(
        id=uuid4(),
        workspace_id=uuid4(),
        user_id=_USER,
        summary="importance restored",
        content="c",
        type="note",
        scope="working",
        client="test",
        importance=0.9,
        embedding_status="success",
    )
    session.add_all([unrebuildable, requeued, restored, rescored])
    report = SleepReport(id=uuid4(), user_id=_USER, status="completed")
    session.add(report)
    await session.flush()
    # Undone newest first: the failing undo runs before two that must still land.
    session.add_all(
        [
            SleepAction(
                report_id=report.id,
                phase="importance_reeval",
                action_type="update_importance",
                memory_id=rescored.id,
                details={"old_importance": 0.4},
            ),
            *(
                SleepAction(
                    report_id=report.id,
                    phase="consolidation",
                    action_type="archive",
                    memory_id=row.id,
                    details={"mode": "tombstone"},
                )
                for row in (restored, requeued, unrebuildable)
            ),
        ]
    )
    await session.commit()
    # Plain ids: a rolled-back savepoint expires the rows it changed.
    unrebuildable_id, requeued_id, restored_id, rescored_id, report_id = (
        unrebuildable.id,
        requeued.id,
        restored.id,
        rescored.id,
        report.id,
    )
    failing = {unrebuildable.summary, requeued.summary}

    async def _embed(text, *_args, **_kwargs):
        if text in failing:
            raise RuntimeError("provider down")
        return [0.1]

    embedding_service = AsyncMock()
    embedding_service.embed = AsyncMock(side_effect=_embed)

    from services.sleep import undo

    real_requeue = undo._requeue_restored

    async def _requeue(db, memory_id):
        if memory_id == unrebuildable_id:
            raise RuntimeError("database down")
        return await real_requeue(db, memory_id)

    async def _get_db():
        yield session

    with (
        patch("db.base.get_db", new=_get_db),
        patch(
            "mcp_server.tools.sleep._authorize_report_rollback",
            new=AsyncMock(return_value=None),
        ),
        patch("services.embedding_service.EmbeddingService", return_value=embedding_service),
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()) as upsert,
        patch("services.content_chunk_writer.write_content_chunks", new=AsyncMock()),
        patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()) as payload,
        patch.object(undo, "_requeue_restored", new=_requeue),
    ):
        response = await handle_rollback_sleep_run({"report_id": str(report.id)}, _USER, None)
    data = json.loads(response[0].text)

    assert data["error"] == "partial_rollback", data
    summary = data["rollback_summary"]
    assert summary["archives_restored"] == 2
    assert summary["points_requeued"] == 1
    assert summary["importance_restored"] == 1
    assert len(summary["errors"]) == 1
    # The real point writer ran on the restored rows (no lazy load after the
    # restore UPDATE): one summary point written, for the row whose embedding answered.
    assert [c.kwargs["memory_id"] for c in upsert.await_args_list] == [restored_id]

    session.expunge_all()
    rows = {
        row.id: row
        for row in (
            await session.execute(
                select(
                    Memory.id, Memory.deleted_at, Memory.embedding_status, Memory.importance
                ).where(Memory.user_id == _USER)
            )
        ).all()
    }

    def _row(memory_id: UUID):
        return rows[memory_id]

    # The failed undo left its tombstone as it was: retryable, not live without a point.
    assert _row(unrebuildable_id).deleted_at is not None
    assert _row(unrebuildable_id).embedding_status == "failed"
    # The others committed, the failed one before them notwithstanding.
    assert _row(requeued_id).deleted_at is None
    assert _row(requeued_id).embedding_status == "pending"
    assert _row(restored_id).deleted_at is None
    assert _row(restored_id).embedding_status == "success"
    assert _row(rescored_id).importance == pytest.approx(0.4)
    assert payload.await_args.kwargs["memory_id"] == rescored_id

    status = (
        await session.execute(select(SleepReport.status).where(SleepReport.id == report_id))
    ).scalar_one()
    assert status == "failed"


@pytest.mark.parametrize("embed_fails", [False, True])
async def test_a_per_merge_undo_writes_the_restored_point(
    session: AsyncSession, embed_fails: bool
) -> None:
    """The per-merge undo re-reads the row its restore UPDATE expired before
    writing the point (an expired attribute cannot be loaded lazily on an
    async session). A point write that fails re-queues the row and the undo
    stands: the embedding sweep rebuilds the point."""
    from models.memory import DELETED_BY_SLEEP_MERGE
    from services.sleep.undo import undo_merge_action

    winner = _archived("merge winner")
    winner.deleted_at = None
    winner.deleted_by = None
    loser = _archived("merge loser")
    loser.deleted_by = DELETED_BY_SLEEP_MERGE
    report = SleepReport(id=uuid4(), user_id=_USER, status="completed")
    session.add_all([winner, loser, report])
    await session.flush()
    action = SleepAction(
        report_id=report.id,
        phase="dedup_merge",
        action_type="merge",
        memory_id=winner.id,
        target_id=loser.id,
        details={},
    )
    session.add(action)
    await session.commit()
    loser_id, action_id = loser.id, action.id

    embedding_service = AsyncMock()
    embedding_service.embed = AsyncMock(
        side_effect=RuntimeError("provider down") if embed_fails else None,
        return_value=[0.1],
    )
    with (
        patch("services.embedding_service.EmbeddingService", return_value=embedding_service),
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()) as upsert,
        patch("services.content_chunk_writer.write_content_chunks", new=AsyncMock()),
    ):
        result = await undo_merge_action(session, action_id, acting_user_id=_USER)

    assert result["restored_memory_id"] == str(loser_id)
    session.expunge_all()
    row = (
        await session.execute(
            select(Memory.deleted_at, Memory.embedding_status).where(Memory.id == loser_id)
        )
    ).one()
    assert row.deleted_at is None
    if embed_fails:
        upsert.assert_not_awaited()
        assert row.embedding_status == "pending"
    else:
        assert upsert.await_args.kwargs["memory_id"] == loser_id
        assert row.embedding_status == "success"
