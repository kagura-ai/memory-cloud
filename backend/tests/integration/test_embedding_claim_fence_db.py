"""Overlapping embedding runs against a real Postgres (#2041).

A run that outlives the 60 s stale cutoff is reclaimed by the sweep: the newer
run stamps its own ``embedding_attempted_at`` and the status stays
``processing``. These tests run two ``process_pending_embedding`` calls on one
row, interleaved, and pin that only the newer run's outcome lands: the stale
run's ``success`` must not cover the newer run's failure (the row would never
be retried), and its failure must not cover the newer run's success. The
vector store and the embedding provider are stubbed; skipped when Postgres is
unreachable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from models.memory import Memory
from utils.datetime import utcnow

pytestmark = pytest.mark.asyncio(loop_scope="session")

_USER = "embedding-claim-fence-it"
_TIMEOUT = 15


@pytest_asyncio.fixture(loop_scope="session")
async def row(db_session: AsyncSession) -> AsyncIterator[Memory]:
    memory = Memory(
        id=uuid4(),
        workspace_id=uuid4(),
        user_id=_USER,
        summary="claim fence",
        content="claim fence",
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


async def _final(async_engine: AsyncEngine, memory_id: UUID) -> Memory:
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        return (await session.execute(select(Memory).where(Memory.id == memory_id))).scalar_one()


async def _overlap(
    async_engine: AsyncEngine,
    memory_id: UUID,
    *,
    stale_embed: Callable[[], Awaitable[list[float]]],
    newer_embed: Callable[[], Awaitable[list[float]]],
) -> None:
    """Run W (claims first), then R2 (reclaims W's stale claim), interleaved.

    W blocks inside its embed until R2 has claimed the row; R2 blocks inside
    its embed until W has finished. ``stale_embed`` / ``newer_embed`` decide
    each run's outcome once released.
    """
    from services import memory_service

    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    sessions: list[AsyncSession] = []

    def _get_db() -> AsyncIterator[AsyncSession]:
        async def _gen() -> AsyncIterator[AsyncSession]:
            session = maker()
            sessions.append(session)
            yield session

        return _gen()

    w_embedding, r2_embedding, w_done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def _w_embed(*_a: Any, **_k: Any) -> list[float]:
        w_embedding.set()
        await r2_embedding.wait()
        return await stale_embed()

    async def _r2_embed(*_a: Any, **_k: Any) -> list[float]:
        r2_embedding.set()
        await w_done.wait()
        return await newer_embed()

    w_svc, r2_svc = MagicMock(model="m"), MagicMock(model="m")
    w_svc.embed = AsyncMock(side_effect=_w_embed)
    r2_svc.embed = AsyncMock(side_effect=_r2_embed)

    t0 = utcnow()
    clock = [t0]
    try:
        with (
            patch("db.base.get_db", side_effect=_get_db),
            patch("utils.datetime.utcnow", side_effect=lambda: clock[0]),
            patch("services.embedding_service.EmbeddingService", MagicMock()),
            patch(
                "services.context_routing.resolve_context_routing",
                AsyncMock(side_effect=[("kagura_memories", w_svc), ("kagura_memories", r2_svc)]),
            ),
            patch.object(memory_service, "add_memory_to_qdrant", AsyncMock()),
            patch.object(memory_service, "build_memory_point", return_value=({}, [], [])),
            patch.object(memory_service, "_create_knn_seed_edges", AsyncMock()),
            patch.object(memory_service, "_create_tag_cooccurrence_seed_edges", AsyncMock()),
        ):
            w = asyncio.create_task(memory_service.process_pending_embedding(memory_id))
            await asyncio.wait_for(w_embedding.wait(), _TIMEOUT)
            # W is now past the stale cutoff: the sweep's run reclaims it.
            clock[0] = t0 + timedelta(seconds=61)
            r2 = asyncio.create_task(memory_service.process_pending_embedding(memory_id))
            await asyncio.wait_for(w, _TIMEOUT)
            w_done.set()
            await asyncio.wait_for(r2, _TIMEOUT)
    finally:
        for session in sessions:
            await session.close()
    assert w_svc.embed.await_count == 1
    assert r2_svc.embed.await_count == 1


async def test_a_stale_success_does_not_hide_the_newer_runs_failure(
    async_engine: AsyncEngine, row: Memory
) -> None:
    async def _ok() -> list[float]:
        return [0.1]

    async def _timeout() -> list[float]:
        raise RuntimeError("provider timeout")

    await _overlap(async_engine, row.id, stale_embed=_ok, newer_embed=_timeout)

    final = await _final(async_engine, row.id)
    # Before #2041 the stale run stamped `success` on the newer claim and the
    # newer run's failure write then matched nothing: `success`, never retried.
    assert final.embedding_status == "failed"
    assert "provider timeout" in (final.embedding_error or "")


async def test_a_stale_failure_does_not_cover_the_newer_runs_success(
    async_engine: AsyncEngine, row: Memory
) -> None:
    async def _timeout() -> list[float]:
        raise RuntimeError("provider timeout")

    async def _ok() -> list[float]:
        return [0.1]

    await _overlap(async_engine, row.id, stale_embed=_timeout, newer_embed=_ok)

    final = await _final(async_engine, row.id)
    assert final.embedding_status == "success"
    assert final.embedding_error is None


async def test_a_slow_live_run_keeps_its_claim(async_engine: AsyncEngine, row: Memory) -> None:
    """The run renews its stamp, so the sweep does not reclaim it for being slow."""
    from services import embedding_claim, memory_service

    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    sessions: list[AsyncSession] = []

    def _get_db() -> AsyncIterator[AsyncSession]:
        async def _gen() -> AsyncIterator[AsyncSession]:
            session = maker()
            sessions.append(session)
            yield session

        return _gen()

    embedding, release = asyncio.Event(), asyncio.Event()

    async def _slow_embed(*_a: Any, **_k: Any) -> list[float]:
        embedding.set()
        await release.wait()
        return [0.1]

    svc = MagicMock(model="m")
    svc.embed = AsyncMock(side_effect=_slow_embed)
    routing = AsyncMock(return_value=("kagura_memories", svc))

    t0 = utcnow()
    clock = [t0]
    later = t0 + timedelta(seconds=61)

    async def _stamp() -> object:
        return (await _final(async_engine, row.id)).embedding_attempted_at

    try:
        with (
            patch.object(embedding_claim, "RENEW_INTERVAL_SECONDS", 0.01),
            patch("db.base.get_db", side_effect=_get_db),
            patch("utils.datetime.utcnow", side_effect=lambda: clock[0]),
            patch("services.embedding_service.EmbeddingService", MagicMock()),
            patch("services.context_routing.resolve_context_routing", routing),
            patch.object(memory_service, "add_memory_to_qdrant", AsyncMock()),
            patch.object(memory_service, "build_memory_point", return_value=({}, [], [])),
            patch.object(memory_service, "_create_knn_seed_edges", AsyncMock()),
            patch.object(memory_service, "_create_tag_cooccurrence_seed_edges", AsyncMock()),
        ):
            w = asyncio.create_task(memory_service.process_pending_embedding(row.id))
            await asyncio.wait_for(embedding.wait(), _TIMEOUT)
            # The run is still working 61 s after its claim: it has renewed.
            clock[0] = later

            async def _renewed() -> None:
                while await _stamp() != later:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(_renewed(), _TIMEOUT)
            # The sweep's run finds nothing stale to reclaim.
            await asyncio.wait_for(memory_service.process_pending_embedding(row.id), _TIMEOUT)
            assert routing.await_count == 1
            release.set()
            await asyncio.wait_for(w, _TIMEOUT)
    finally:
        for session in sessions:
            await session.close()

    final = await _final(async_engine, row.id)
    assert final.embedding_status == "success"


async def test_a_run_whose_claim_a_finished_run_took_re_queues_the_row(
    async_engine: AsyncEngine, row: Memory
) -> None:
    """#2061: a newer run reclaimed the row and finished while this run was
    still writing, so this run's points may have replaced the newer run's.
    Its fenced success write matches nothing, and the row goes back to
    ``pending`` for the sweep; the newer run's stamp is left as it is."""
    from sqlalchemy import update

    from services import memory_service

    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    sessions: list[AsyncSession] = []

    def _get_db() -> AsyncIterator[AsyncSession]:
        async def _gen() -> AsyncIterator[AsyncSession]:
            session = maker()
            sessions.append(session)
            yield session

        return _gen()

    newer_stamp = utcnow() + timedelta(seconds=61)

    async def _embed(*_a: Any, **_k: Any) -> list[float]:
        async with maker() as other:
            await other.execute(
                update(Memory)
                .where(Memory.id == row.id)
                .values(embedding_status="success", embedding_attempted_at=newer_stamp)
            )
            await other.commit()
        return [0.1]

    svc = MagicMock(model="m")
    svc.embed = AsyncMock(side_effect=_embed)
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
            await asyncio.wait_for(memory_service.process_pending_embedding(row.id), _TIMEOUT)
    finally:
        for session in sessions:
            await session.close()

    final = await _final(async_engine, row.id)
    assert final.embedding_status == "pending"
    assert final.embedding_attempted_at == newer_stamp
    knn.assert_not_awaited()


# --------------------------------------------------------------------- #2095
# A reclaim of a stale `processing` row spends one retry, so a run that
# outlives its claim every time ends instead of looping forever: the claim
# stops matching once the budget is spent, and the sweep fails the row.


async def _make_stale(async_engine: AsyncEngine, memory_id: UUID, retry_count: int) -> None:
    from sqlalchemy import update

    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        await session.execute(
            update(Memory)
            .where(Memory.id == memory_id)
            .values(
                embedding_status="processing",
                embedding_attempted_at=utcnow() - timedelta(seconds=600),
                embedding_retry_count=retry_count,
            )
        )
        await session.commit()


def _session_factory(async_engine: AsyncEngine, sessions: list[AsyncSession]):
    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    def _get_db() -> AsyncIterator[AsyncSession]:
        async def _gen() -> AsyncIterator[AsyncSession]:
            session = maker()
            sessions.append(session)
            yield session

        return _gen()

    return _get_db


async def test_a_stale_reclaim_spends_one_retry_and_success_resets_it(
    async_engine: AsyncEngine, row: Memory
) -> None:
    from services import memory_service

    await _make_stale(async_engine, row.id, retry_count=2)
    seen: list[int] = []

    async def _embed(*_a: Any, **_k: Any) -> list[float]:
        seen.append((await _final(async_engine, row.id)).embedding_retry_count)
        return [0.1]

    svc = MagicMock(model="m")
    svc.embed = AsyncMock(side_effect=_embed)
    sessions: list[AsyncSession] = []
    try:
        with (
            patch("db.base.get_db", side_effect=_session_factory(async_engine, sessions)),
            patch("services.embedding_service.EmbeddingService", MagicMock()),
            patch(
                "services.context_routing.resolve_context_routing",
                AsyncMock(return_value=("kagura_memories", svc)),
            ),
            patch.object(memory_service, "add_memory_to_qdrant", AsyncMock()),
            patch.object(memory_service, "build_memory_point", return_value=({}, [], [])),
            patch.object(memory_service, "_create_knn_seed_edges", AsyncMock()),
            patch.object(memory_service, "_create_tag_cooccurrence_seed_edges", AsyncMock()),
        ):
            await asyncio.wait_for(memory_service.process_pending_embedding(row.id), _TIMEOUT)
    finally:
        for session in sessions:
            await session.close()

    assert seen == [3]  # the reclaim spent one retry
    final = await _final(async_engine, row.id)
    assert final.embedding_status == "success"
    assert final.embedding_retry_count == 0  # the budget is per failure episode


async def test_an_exhausted_stale_row_is_not_claimed_and_the_sweep_fails_it(
    async_engine: AsyncEngine, row: Memory
) -> None:
    from services import memory_service
    from services.embedding_claim import STALE_CLAIM_SECONDS
    from tasks.embedding_tasks import CLAIM_EXPIRED_ERROR, _fail_exhausted_reclaims

    await _make_stale(async_engine, row.id, retry_count=3)
    before = await _final(async_engine, row.id)
    routing = AsyncMock()
    sessions: list[AsyncSession] = []
    try:
        with (
            patch("db.base.get_db", side_effect=_session_factory(async_engine, sessions)),
            patch("services.context_routing.resolve_context_routing", routing),
        ):
            await asyncio.wait_for(memory_service.process_pending_embedding(row.id), _TIMEOUT)
    finally:
        for session in sessions:
            await session.close()

    routing.assert_not_awaited()
    unclaimed = await _final(async_engine, row.id)
    assert unclaimed.embedding_status == "processing"
    assert unclaimed.embedding_attempted_at == before.embedding_attempted_at
    assert unclaimed.embedding_retry_count == 3

    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    now = utcnow()
    async with maker() as session:
        await _fail_exhausted_reclaims(session, now - timedelta(seconds=STALE_CLAIM_SECONDS), now)

    final = await _final(async_engine, row.id)
    assert final.embedding_status == "failed"
    assert final.embedding_error == CLAIM_EXPIRED_ERROR
    assert final.embedding_retry_count == 3
    assert final.embedding_attempted_at == now


async def test_the_exhaust_step_leaves_a_live_or_budgeted_row(
    async_engine: AsyncEngine, row: Memory
) -> None:
    """A fresh claim (a live run) and a stale row with budget left are not failed."""
    from sqlalchemy import update

    from services.embedding_claim import STALE_CLAIM_SECONDS
    from tasks.embedding_tasks import _fail_exhausted_reclaims

    maker = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    now = utcnow()
    cutoff = now - timedelta(seconds=STALE_CLAIM_SECONDS)
    for stamp, count in ((now, 3), (now - timedelta(seconds=600), 2)):
        async with maker() as session:
            await session.execute(
                update(Memory)
                .where(Memory.id == row.id)
                .values(
                    embedding_status="processing",
                    embedding_attempted_at=stamp,
                    embedding_retry_count=count,
                )
            )
            await session.commit()
            await _fail_exhausted_reclaims(session, cutoff, now)
        assert (await _final(async_engine, row.id)).embedding_status == "processing"
