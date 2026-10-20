"""#2063: an embedding-model switch re-queues a memory a Sleep undo restored
after ``--verify``.

The re-embed skips tombstones, so the target collection has no point for a
merge loser or a consolidation archive. A per-merge undo or a run-level
rollback between ``--verify`` and ``--switch`` brings the row back and
re-embeds it into the collection the context still routes to. The restore
stamps the embedding pipeline's clock (``embedding_attempted_at``) and the
switch re-queues rows embedded since the re-embed started, so the sweep embeds
the row under the new routing. ``updated_at`` is not touched: Sleep dedup reads
it as the fact's currency.

Real Postgres: the restore UPDATE, the switch predicates and the re-queue
UPDATE all run as SQL.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from models.auth import Context, User, Workspace, WorkspaceMember, WorkspaceRole
from models.memory import DELETED_BY_SLEEP_ARCHIVE, DELETED_BY_SLEEP_MERGE, Memory
from models.sleep import SleepAction, SleepReport
from services import embedding_migration_service as svc
from utils.datetime import utcnow

TARGET = "qwen3-embedding:4b"
TARGET_DIMENSIONS = 2560


async def _scope(db, owner: str) -> tuple[Workspace, Context]:
    db.add(User(email=f"{owner}@test.example", user_id=owner, role="user"))
    await db.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"mig-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db.add(ws)
    await db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=owner, role=WorkspaceRole.OWNER))
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"mig-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
        trust_tier="trusted",
    )
    db.add(ctx)
    await db.flush()
    return ws, ctx


def _row(owner: str, ws: Workspace, ctx: Context, *, written: datetime, **extra) -> Memory:
    """Embedded and last written before the re-embed started."""
    values = {
        "embedding_status": "success",
        "created_at": written,
        "embedding_attempted_at": written,
        **extra,
    }
    return Memory(
        id=uuid4(),
        workspace_id=ws.id,
        context_id=ctx.id,
        user_id=owner,
        summary="written before the re-embed",
        content="c",
        type="note",
        client="pytest",
        scope="working",
        **values,
    )


async def _count(db, ids, predicate) -> int:
    stmt = select(func.count()).select_from(Memory).where(Memory.id.in_(ids), predicate)
    return int((await db.execute(stmt)).scalar_one())


@pytest.mark.asyncio
async def test_switch_requeues_rows_restored_after_verify(db_session):
    from mcp_server.tools.sleep import _RollbackCtx, _undo_archive
    from services.sleep.undo import undo_merge_action

    owner = f"mig-user-{uuid4().hex[:8]}"
    ws, ctx = await _scope(db_session, owner)
    written = utcnow() - timedelta(days=30)
    tombstoned = utcnow() - timedelta(days=2)
    loser = _row(
        owner, ws, ctx, written=written, deleted_at=tombstoned, deleted_by=DELETED_BY_SLEEP_MERGE
    )
    archive = _row(
        owner, ws, ctx, written=written, deleted_at=tombstoned, deleted_by=DELETED_BY_SLEEP_ARCHIVE
    )
    # Archived while its embedding had failed: restored, but its clock is the
    # retry backoff's, and its status re-queues it anyway.
    failed_archive = _row(
        owner,
        ws,
        ctx,
        written=written,
        deleted_at=tombstoned,
        deleted_by=DELETED_BY_SLEEP_ARCHIVE,
        embedding_status="failed",
    )
    untouched = _row(owner, ws, ctx, written=written)
    db_session.add_all([loser, archive, failed_archive, untouched])
    report = SleepReport(
        id=uuid4(),
        user_id=owner,
        workspace_id=ws.id,
        context_id=ctx.id,
        status="completed",
    )
    db_session.add(report)
    await db_session.flush()
    merge = SleepAction(
        report_id=report.id,
        phase="dedup_merge",
        action_type="merge",
        memory_id=untouched.id,
        target_id=loser.id,
        details={},
    )
    db_session.add(merge)
    await db_session.commit()
    ids = [loser.id, archive.id, failed_archive.id, untouched.id]

    # The re-embed started (and --verify passed) after every write above.
    requeue_since = utcnow() - timedelta(seconds=1)
    assert await _count(db_session, [untouched.id], svc._left_by_switch(requeue_since)) == 1

    # Per-merge undo of the merge loser.
    reembedded = []

    async def capture(memory, *args, **kwargs):
        reembedded.append(memory.id)

    with (
        patch("services.sleep.undo.re_embed_memory_to_qdrant", side_effect=capture),
        patch("services.embedding_service.EmbeddingService", MagicMock()),
    ):
        await undo_merge_action(db_session, merge.id, acting_user_id=owner)
    assert reembedded == [loser.id]

    # Run-level rollback of two consolidation archives.
    rollback_ctx = _RollbackCtx(
        user_id=owner,
        ws_id=str(ws.id),
        ctx_id_str=str(ctx.id),
        collection_name="kagura_memories",
        embedding_svc=MagicMock(),
        memory_cache={archive.id: archive, failed_archive.id: failed_archive},
        edge_repo=MagicMock(),
    )
    summary = {"archives_restored": 0, "errors": []}
    with patch("mcp_server.tools.sleep._re_embed_to_qdrant", new_callable=AsyncMock):
        for memory_id in (archive.id, failed_archive.id):
            action = MagicMock()
            action.action_type = "archive"
            action.memory_id = memory_id
            action.details = {"mode": "tombstone"}
            await _undo_archive(db_session, action, rollback_ctx, summary)
    await db_session.commit()
    assert summary == {"archives_restored": 2, "errors": []}

    rows = {
        row.id: row
        for row in (
            await db_session.execute(
                select(
                    Memory.id,
                    Memory.deleted_at,
                    Memory.updated_at,
                    Memory.embedding_attempted_at,
                ).where(Memory.id.in_(ids))
            )
        ).all()
    }
    for memory_id in (loser.id, archive.id):
        assert rows[memory_id].deleted_at is None
        assert rows[memory_id].updated_at is None  # dedup recency unchanged
        assert rows[memory_id].embedding_attempted_at >= requeue_since
    assert rows[failed_archive.id].deleted_at is None
    assert rows[failed_archive.id].embedding_attempted_at == written

    restored = [loser.id, archive.id, failed_archive.id]
    assert await _count(db_session, restored, svc._requeued_by_switch(requeue_since)) == 3
    assert await _count(db_session, restored, svc._left_by_switch(requeue_since)) == 0
    assert await _count(db_session, [untouched.id], svc._left_by_switch(requeue_since)) == 1

    with (
        patch.object(svc, "delete_points_from_qdrant", AsyncMock()),
        patch.object(svc, "delete_chunk_points", AsyncMock()),
    ):
        result = await svc.switch_context_embedding(
            db_session, ctx.id, TARGET, TARGET_DIMENSIONS, requeue_since=requeue_since
        )

    assert result.requeued == 3
    statuses = dict(
        (
            await db_session.execute(
                select(Memory.id, Memory.embedding_status).where(Memory.id.in_(ids))
            )
        )
        .tuples()
        .all()
    )
    assert statuses == {
        loser.id: "pending",
        archive.id: "pending",
        failed_archive.id: "pending",
        untouched.id: "success",
    }
