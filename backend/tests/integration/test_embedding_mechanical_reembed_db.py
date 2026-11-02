"""A mechanical re-queue skips supersede detection, an edit does not (#2092).

Three ``process_pending_embedding`` runs on one row against a real Postgres:
the first embed detects and stamps ``embedded_text_digest``; a repair re-queue
of the unchanged text is a mechanical re-embed; an edit moves the digest and
detects again. #2256: a mechanical re-embed of a row holding a live candidate
still clears it when the pair is no longer near (and keeps it when it is).
The vector store and the embedding provider are stubbed; skipped when
Postgres is unreachable.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.auth import Context, User, Workspace
from models.memory import Memory
from neural.config import NeuralMemoryConfig
from services.supersede_verify import embedded_text_digest
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_USER = "embedding-mechanical-reembed-it"


@pytest_asyncio.fixture(loop_scope="session")
async def row(db_session: AsyncSession) -> AsyncIterator[Memory]:
    memory = Memory(
        id=uuid4(),
        workspace_id=uuid4(),
        user_id=_USER,
        summary="mechanical summary",
        content="mechanical content",
        type="note",
        scope="working",
        client="test",
        created_at=utcnow(),
        embedding_status="pending",
    )
    db_session.add(memory)
    await db_session.commit()
    yield memory
    await db_session.rollback()
    await db_session.execute(delete(Memory).where(Memory.user_id == _USER))
    await db_session.commit()


def _maker(async_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


async def _final(async_engine: AsyncEngine, memory_id: UUID) -> Memory:
    async with _maker(async_engine)() as session:
        return (await session.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()


async def _embed_once(async_engine: AsyncEngine, memory_id: UUID) -> bool:
    """Run the pipeline once; return the ``mechanical_reembed`` it passed on."""
    from services import memory_service

    maker = _maker(async_engine)
    sessions: list[AsyncSession] = []

    def _get_db() -> AsyncIterator[AsyncSession]:
        async def _gen() -> AsyncIterator[AsyncSession]:
            session = maker()
            sessions.append(session)
            yield session

        return _gen()

    svc = MagicMock(model="m")
    svc.embed = AsyncMock(return_value=[0.1])
    try:
        with (
            patch("db.base.get_db", side_effect=_get_db),
            patch("services.embedding_service.EmbeddingService", MagicMock()),
            patch(
                "services.context_routing.resolve_context_routing",
                AsyncMock(return_value=("kagura_memories", svc)),
            ),
            patch.object(memory_service, "add_memory_to_qdrant", AsyncMock()),
            patch.object(memory_service, "build_memory_point", return_value=({}, [], [])),
            patch.object(memory_service, "_create_knn_seed_edges", AsyncMock()) as knn,
            patch.object(memory_service, "_create_tag_cooccurrence_seed_edges", AsyncMock()),
        ):
            await memory_service.process_pending_embedding(memory_id)
    finally:
        for session in sessions:
            await session.close()
    knn.assert_awaited_once()
    return bool(knn.await_args.kwargs["mechanical_reembed"])


async def test_backfill_requeue_skips_detection_but_an_edit_does_not(
    async_engine: AsyncEngine, row: Memory
) -> None:
    from services.content_chunk_writer import requeue_embedding

    # 1. First embed: NULL digest → detection runs; success stamps the digest.
    assert await _embed_once(async_engine, row.id) is False
    first = await _final(async_engine, row.id)
    assert first.embedding_status == "success"
    assert first.embedded_text_digest == embedded_text_digest(
        "mechanical summary", "mechanical content"
    )

    # 2. A repair re-queue of the unchanged text → mechanical.
    async with _maker(async_engine)() as session:
        assert await requeue_embedding(session, row.id, reason="test") is True
        await session.commit()
    assert await _embed_once(async_engine, row.id) is True
    assert (await _final(async_engine, row.id)).embedded_text_digest == first.embedded_text_digest

    # 3. An edit re-queues with new text → detection runs and the stamp moves.
    async with _maker(async_engine)() as session:
        await session.execute(
            update(Memory)
            .where(Memory.id == row.id)
            .values(summary="edited summary", embedding_status="pending")
        )
        await session.commit()
    assert await _embed_once(async_engine, row.id) is False
    edited = await _final(async_engine, row.id)
    assert edited.embedding_status == "success"
    assert edited.embedded_text_digest == embedded_text_digest(
        "edited summary", "mechanical content"
    )


@pytest.fixture
def suggest_env(monkeypatch: pytest.MonkeyPatch):
    """Supersede suggestions on (judge off), seed edges off."""
    monkeypatch.setenv("KNN_SEED_ENABLED", "false")
    monkeypatch.setenv("SUPERSEDE_SUGGEST_ENABLED", "true")
    monkeypatch.setenv("SUPERSEDE_VERIFY_MODE", "off")
    NeuralMemoryConfig.invalidate_cache()
    yield
    NeuralMemoryConfig.invalidate_cache()


@pytest_asyncio.fixture(loop_scope="session")
async def candidate_scope(db_session: AsyncSession) -> AsyncIterator[dict[str, Any]]:
    owner = f"mechclear_{uuid4().hex[:8]}"
    db_session.add(User(email=f"{owner}@test.example", user_id=owner, name=owner, role="user"))
    await db_session.flush()
    ws = Workspace(
        id=uuid4(),
        name=f"mechclear-ws-{uuid4().hex[:8]}",
        plan_name="pro",
        owner_user_id=owner,
        daily_api_limit=5000,
        weekly_api_limit=25000,
    )
    db_session.add(ws)
    await db_session.flush()
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"mechclear-ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
    )
    db_session.add(ctx)
    await db_session.flush()

    def _memory(summary: str) -> Memory:
        return Memory(
            id=uuid4(),
            workspace_id=ws.id,
            context_id=ctx.id,
            user_id=owner,
            summary=summary,
            content=summary,
            type="note",
            scope="working",
            client="test",
            created_at=utcnow(),
            embedding_status="pending",
        )

    target = _memory("mechanical clear: the older near-duplicate")
    target.embedding_status = "success"
    row = _memory("mechanical clear: the row holding the candidate")
    db_session.add_all([target, row])
    await db_session.commit()
    yield {"row": row, "target": target}
    await db_session.rollback()
    await db_session.execute(delete(Memory).where(Memory.user_id == owner))
    await db_session.execute(delete(Context).where(Context.id == ctx.id))
    await db_session.execute(delete(Workspace).where(Workspace.id == ws.id))
    await db_session.execute(delete(User).where(User.user_id == owner))
    await db_session.commit()


async def _embed_with_detection(
    async_engine: AsyncEngine,
    memory_id: UUID,
    hits: list[dict[str, Any]],
    stored: dict[str, list[float]] | None = None,
) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    """Run the pipeline with the real ``_create_knn_seed_edges``."""
    from services import memory_service

    maker = _maker(async_engine)
    sessions: list[AsyncSession] = []

    def _get_db() -> AsyncIterator[AsyncSession]:
        async def _gen() -> AsyncIterator[AsyncSession]:
            session = maker()
            sessions.append(session)
            yield session

        return _gen()

    svc = MagicMock(model="m")
    svc.embed = AsyncMock(return_value=[1.0, 0.0])
    judge = AsyncMock()
    retrieve = AsyncMock(return_value=stored or {})
    try:
        with (
            patch("db.base.get_db", side_effect=_get_db),
            patch("services.embedding_service.EmbeddingService", MagicMock()),
            patch(
                "services.context_routing.resolve_context_routing",
                AsyncMock(return_value=("kagura_memories", svc)),
            ),
            patch.object(memory_service, "add_memory_to_qdrant", AsyncMock()),
            patch.object(memory_service, "build_memory_point", return_value=({}, [], [])),
            patch("db.qdrant.search_memories_qdrant", AsyncMock(return_value=hits)) as search,
            patch("db.qdrant.retrieve_dense_vectors", retrieve),
            patch("services.supersede_verify.judge_pair", judge),
            patch.object(memory_service, "_create_tag_cooccurrence_seed_edges", AsyncMock()),
        ):
            await memory_service.process_pending_embedding(memory_id)
    finally:
        for session in sessions:
            await session.close()
    return search, judge, retrieve


async def test_mechanical_reembed_clears_a_candidate_without_a_near_neighbour(
    async_engine: AsyncEngine, candidate_scope: dict[str, Any], suggest_env: None
) -> None:
    from services.content_chunk_writer import requeue_embedding

    row: Memory = candidate_scope["row"]
    target: Memory = candidate_scope["target"]
    own = {"id": str(row.id), "score": 1.0, "payload": {}}
    near = {"id": str(target.id), "score": 0.90, "payload": {}}

    # First embed stamps the digest (and stores the near pair as the candidate).
    await _embed_with_detection(async_engine, row.id, [own, near])
    first = await _final(async_engine, row.id)
    digest = first.embedded_text_digest
    assert digest == embedded_text_digest(row.summary, row.content)
    assert first.supersede_candidate["memory_id"] == str(target.id)

    async def requeue() -> None:
        async with _maker(async_engine)() as session:
            assert await requeue_embedding(session, row.id, reason="test") is True
            await session.commit()

    # 2. A repair re-queue (mechanical) while the pair is still near: kept.
    # The pair is measured on the target's stored vector, not the search.
    await requeue()
    search, judge, retrieve = await _embed_with_detection(
        async_engine, row.id, [own], {str(target.id): [0.9, math.sqrt(1 - 0.81)]}
    )
    search.assert_not_called()
    retrieve.assert_awaited_once_with("kagura_memories", [str(target.id)])
    judge.assert_not_called()
    kept = await _final(async_engine, row.id)
    assert kept.supersede_candidate == first.supersede_candidate
    assert kept.embedded_text_digest == digest

    # 3. The target has no point in the collection yet (a model switch fills
    # the new collection row by row): no evidence, kept.
    await requeue()
    await _embed_with_detection(async_engine, row.id, [own], {})
    assert (await _final(async_engine, row.id)).supersede_candidate == first.supersede_candidate

    # 4. The re-embedded pair scores far lower (a model switch): cleared, no
    # judge, no store.
    await requeue()
    _, judge, _ = await _embed_with_detection(
        async_engine, row.id, [own], {str(target.id): [0.5, math.sqrt(1 - 0.25)]}
    )
    judge.assert_not_called()
    cleared = await _final(async_engine, row.id)
    assert cleared.supersede_candidate is None
    assert cleared.embedded_text_digest == digest
    assert cleared.embedding_status == "success"
