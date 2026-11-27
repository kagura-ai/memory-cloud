"""#3036: Sleep reindex of a metadata-only change on a row without keyword lengths.

A memory embedded before its keyword length columns existed has them NULL.
A metadata-only change (an importance re-evaluation) sends it through the
reindex, whose point writer computes the lengths. Stored on the ORM row, the
lengths made the row dirty: the next savepoint flushed an UPDATE, which
expired the row's generated columns, and the chunk payload sync read
``resource_id`` as a lazy load from plain async code (``MissingGreenlet``).
The memory was counted as failed and its chunk points kept their old payload.

A real ``AsyncSession`` on PostgreSQL; the embedding provider and the vector
store are stubbed. Skipped when Postgres is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.memory import Memory
from services.keyword_stats import MEMORY_LENGTH_COLUMNS
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_USER = "sleep-reindex-keyword-lengths-it"


def _maker(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture(loop_scope="session")
async def rows(async_engine: AsyncEngine) -> AsyncIterator[list[Memory]]:
    """Embedded rows with NULL keyword lengths whose importance then changed."""
    workspace_id = uuid4()
    memories = [
        Memory(
            id=uuid4(),
            workspace_id=workspace_id,
            user_id=_USER,
            summary=f"reindex keyword lengths {index}",
            content=f"reindex keyword lengths content {index}",
            type="note",
            scope="working",
            client="test",
            importance=0.5,
            created_at=utcnow(),
            embedding_status="success",
        )
        for index in range(2)
    ]
    async with _maker(async_engine)() as session:
        session.add_all(memories)
        await session.commit()
        # The metadata-only change of an earlier Sleep phase.
        await session.execute(
            update(Memory)
            .where(Memory.id.in_([m.id for m in memories]))
            .values(importance=0.9)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    yield memories
    async with _maker(async_engine)() as session:
        await session.execute(delete(Memory).where(Memory.user_id == _USER))
        await session.commit()


def _embedding_service() -> MagicMock:
    service = MagicMock(provider="openai", model="text-embedding-3-small")
    service.embed_with_usage = AsyncMock(return_value=([0.1, 0.2, 0.3], 7))
    return service


async def _reindex(async_engine: AsyncEngine, memories: list[Memory], **patches):
    from services.sleep.reindex import ReindexPhase

    ids = {m.id for m in memories}
    sync = AsyncMock(return_value=True)
    async with _maker(async_engine)() as sleep_db:
        with (
            patch("services.sleep.reindex.EmbeddingService", return_value=_embedding_service()),
            patch("services.memory_service.add_memory_to_qdrant", AsyncMock()),
            patch(
                "services.content_chunk_writer.content_chunks_enabled",
                AsyncMock(return_value=True),
            ),
            patch("services.content_chunk_writer.chunk_points_supported", return_value=True),
            patch("services.content_chunk_writer.sync_chunk_points_payload", sync),
        ):
            result = await ReindexPhase(sleep_db).execute(
                ids,
                _USER,
                workspace_id=str(memories[0].workspace_id),
                content_changed_memory_ids=set(),
            )
        await sleep_db.commit()
    return result, sync


async def test_metadata_only_reindex_of_rows_without_lengths_succeeds(
    async_engine: AsyncEngine, rows: list[Memory]
) -> None:
    async with _maker(async_engine)() as session:
        stored = (
            await session.execute(
                select(*(getattr(Memory, c) for c in MEMORY_LENGTH_COLUMNS.values())).where(
                    Memory.id == rows[0].id
                )
            )
        ).one()
    assert all(value is None for value in stored), "the precondition: no stored lengths"

    result, sync = await _reindex(async_engine, rows)

    assert result.details["failed"] == 0, result.details
    assert result.details["reindexed"] == len(rows)
    assert result.success is True
    # The chunk points of each memory got the new payload.
    assert sync.await_count == len(rows)
    # The lengths the point was weighted with are stored on the rows.
    async with _maker(async_engine)() as session:
        for memory in rows:
            lengths = (
                await session.execute(
                    select(*(getattr(Memory, c) for c in MEMORY_LENGTH_COLUMNS.values())).where(
                        Memory.id == memory.id
                    )
                )
            ).one()
            assert any(value is not None for value in lengths), lengths
