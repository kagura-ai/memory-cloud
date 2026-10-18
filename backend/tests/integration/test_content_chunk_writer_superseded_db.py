"""The chunk writer's superseded-run check against a real Postgres (#2041).

Two overlapping re-embeds of one memory write the same chunk point ids, so
``write_content_chunks`` re-reads the row right before its upsert. These tests
pin the SQL of that check (``_superseded``): the NULL claim stamp of a row
never claimed, a re-queue to ``pending``, a newer claim, a content edit and a
soft delete. No Qdrant is needed. Skipped when Postgres is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from models.memory import Memory
from services import content_chunk_writer
from services.content_chunk_writer import _superseded
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_USER = "chunk-superseded-it"
_CONTENT = "x" * 4000


@pytest_asyncio.fixture(loop_scope="session")
async def row(db_session: AsyncSession) -> AsyncIterator[Memory]:
    from uuid import uuid4

    memory = Memory(
        id=uuid4(),
        workspace_id=uuid4(),
        user_id=_USER,
        summary="superseded check",
        content=_CONTENT,
        type="note",
        scope="working",
        client="test",
        created_at=utcnow(),
        embedding_status="processing",
        embedding_attempted_at=datetime(2026, 1, 2, 3, 4, 5, 678901),
    )
    db_session.add(memory)
    await db_session.commit()
    yield memory
    await db_session.rollback()
    await db_session.execute(delete(Memory).where(Memory.user_id == _USER))
    await db_session.commit()


async def _set(db: AsyncSession, memory: Memory, **values) -> None:
    await db.execute(update(Memory).where(Memory.id == memory.id).values(**values))
    await db.commit()


async def test_the_current_run_is_not_superseded(db_session, row):
    claimed = row.embedding_attempted_at
    assert await _superseded(db_session, row.id, _CONTENT, claimed) is False


async def test_a_row_never_claimed_matches_its_null_stamp(db_session, row):
    """Sleep reindex / undo on a row the pipeline never stamped."""
    await _set(db_session, row, embedding_attempted_at=None, embedding_status="success")
    assert await _superseded(db_session, row.id, _CONTENT, None) is False


async def test_an_edit_that_re_queued_the_row_supersedes(db_session, row):
    await _set(db_session, row, embedding_status="pending")
    assert await _superseded(db_session, row.id, _CONTENT, row.embedding_attempted_at) is True


async def test_a_newer_claim_supersedes(db_session, row):
    claimed = row.embedding_attempted_at
    await _set(db_session, row, embedding_attempted_at=claimed + timedelta(seconds=61))
    assert await _superseded(db_session, row.id, _CONTENT, claimed) is True


async def test_a_content_change_supersedes(db_session, row):
    claimed = row.embedding_attempted_at
    await _set(db_session, row, content=_CONTENT + " edited")
    assert await _superseded(db_session, row.id, _CONTENT, claimed) is True


async def test_a_soft_delete_supersedes(db_session, row):
    claimed = row.embedding_attempted_at
    await _set(db_session, row, deleted_at=utcnow())
    assert await _superseded(db_session, row.id, _CONTENT, claimed) is True


async def test_a_failed_check_leaves_the_session_usable(db_session, row):
    # On Postgres a failed statement aborts the transaction; the check runs in
    # a SAVEPOINT so the later checks and the caller's commit still work.
    class _FailingSelect:
        def where(self, *_a):
            return text("SELECT 1 / 0")

    with patch.object(content_chunk_writer, "select", lambda *_a: _FailingSelect()):
        with pytest.raises(DBAPIError):
            await _superseded(db_session, row.id, _CONTENT, row.embedding_attempted_at)
    assert (await db_session.execute(text("SELECT 1"))).scalar_one() == 1
    assert await _superseded(db_session, row.id, _CONTENT, row.embedding_attempted_at) is False
