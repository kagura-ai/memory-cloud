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
