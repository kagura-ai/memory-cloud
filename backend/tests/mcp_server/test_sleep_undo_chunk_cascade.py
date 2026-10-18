"""#2015: the Sleep rollback's importance restore skips the chunk cascade on a
context without content chunks."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools import sleep as sleep_tools


def _ctx(ctx_id_str: str):
    return MagicMock(user_id="u", ctx_id_str=ctx_id_str, collection_name="coll")


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, True])
async def test_importance_restore_cascades_by_the_context_flag(flag):
    action = MagicMock(memory_id=uuid4(), details={"old_importance": 0.4})
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    summary = {"importance_restored": 0, "importance_kept": 0}
    with (
        patch("db.qdrant.update_memory_payload_in_qdrant", new=AsyncMock()) as update,
        patch(
            "services.content_chunk_writer.chunk_cascade_needed",
            new=AsyncMock(return_value=flag),
        ),
    ):
        await sleep_tools._undo_update_importance(db, action, _ctx(str(uuid4())), summary)
    assert update.await_args.kwargs["cascade_chunks"] is flag
    assert summary["importance_restored"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requeued", "chunks", "points"), [("chunks", 1, 0), ("point", 0, 1), (None, 0, 0)]
)
async def test_restore_counts_memories_re_queued_for_their_chunks_or_point(
    requeued, chunks, points
):
    """#2061: a restored memory whose chunk write failed is re-queued and
    counted under ``chunks_requeued``; #2065: one whose point write failed
    under ``points_requeued``. The restore itself still counts."""
    memory_id = uuid4()
    mem = MagicMock(id=memory_id)
    ctx = MagicMock(
        user_id="u",
        ctx_id_str=str(uuid4()),
        collection_name="coll",
        memory_cache={memory_id: mem},
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    summary = {"archives_restored": 0, "chunks_requeued": 0, "points_requeued": 0, "errors": []}
    with (
        patch.object(sleep_tools, "hold_point_writer_lock", new=AsyncMock()),
        patch.object(
            sleep_tools, "_re_embed_to_qdrant", new=AsyncMock(return_value=requeued)
        ) as reembed,
    ):
        await sleep_tools._restore_sleep_tombstone(
            db,
            memory_id,
            ctx,
            summary,
            deleted_by="sleep_consolidation",
            counter_key="archives_restored",
            not_restorable="gone",
        )
    assert reembed.await_args.kwargs["db"] is db
    assert summary == {
        "archives_restored": 1,
        "chunks_requeued": chunks,
        "points_requeued": points,
        "errors": [],
    }


@pytest.mark.asyncio
async def test_re_embed_wrapper_forwards_the_requeue_outcome():
    with patch(
        "services.sleep.undo.re_embed_memory_to_qdrant", new=AsyncMock(return_value="point")
    ) as helper:
        assert (
            await sleep_tools._re_embed_to_qdrant(
                MagicMock(), "u", MagicMock(), "w", "c", "coll", db="db"
            )
            == "point"
        )
    assert helper.await_args.kwargs["db"] == "db"
