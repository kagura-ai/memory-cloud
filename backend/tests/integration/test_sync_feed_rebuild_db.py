"""Unchanged feed rows rebuild their own point under the feed fence (#2908).

Real PostgreSQL sessions on an enrolled ``feed`` target. The embedding
pipeline (``process_pending_embedding`` → ``write_memory_point`` →
``ResourceIndexer.rebuild_point``) may write a managed row's point only when
the row is a live feed row whose stored and in-hand content and resource
version equal its canonical sync record. That test runs under the shared
fence the guard takes, so a transition cannot interleave. Everything else
stays refused with ``SYNC-LOCKED``, and a refused row keeps its retry budget
instead of ending ``failed`` for good.

The vector store and the embedding provider are stubbed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, update

from config.constants import MAX_EMBEDDING_RETRIES
from models.auth import Context, User
from models.erasure import STATUS_CANCELLED, STATUS_COOLING_OFF, ErasureRequest
from models.memory import Memory
from models.sync_feed import SyncRecord
from services import resource_indexer
from services.sync_managed_guard import target_fence
from tests.integration import sync_feed_fixtures as fixtures
from tests.integration import test_sync_management_db as management
from tests.integration.sync_feed_fixtures import committed, human, product
from utils.datetime import utcnow
from utils.exceptions import ConflictError, ManagedDataLockedError
from utils.hashing import sha256_hex

pytestmark = pytest.mark.asyncio(loop_scope="session")
store, worker, feeds = fixtures.store, fixtures.worker, fixtures.feeds
managed = management.managed
apply_pending, stop, finalize = management.apply_pending, management.stop, management.finalize


def ctx_of(worker) -> UUID:
    return UUID(worker.target["context_id"])


async def live_rows(worker) -> list[Memory]:
    async with worker.factory() as db:
        return list(
            (
                await db.scalars(
                    select(Memory)
                    .where(Memory.context_id == ctx_of(worker), Memory.deleted_at.is_(None))
                    .order_by(Memory.id)
                )
            ).all()
        )


async def statuses(worker, ids: Iterable[UUID]) -> dict[UUID, tuple]:
    async with worker.factory() as db:
        rows = (
            await db.execute(
                select(
                    Memory.id,
                    Memory.embedding_status,
                    Memory.embedding_retry_count,
                    Memory.embedding_error,
                ).where(Memory.id.in_(list(ids)))
            )
        ).all()
    return {
        row.id: (row.embedding_status, row.embedding_retry_count, row.embedding_error)
        for row in rows
    }


def stubs(monkeypatch) -> MagicMock:
    """The vector store and the embedding provider; returns the vector store."""
    qdrant = MagicMock()
    qdrant.upsert, qdrant.delete = AsyncMock(), AsyncMock()
    monkeypatch.setattr(resource_indexer, "get_qdrant_client", lambda: qdrant)
    return qdrant


def embedding() -> MagicMock:
    service = MagicMock(model="m")
    service.embed = AsyncMock(return_value=[0.1] * 1536)
    return service


async def run_pipeline(worker, memory_ids: Iterable[UUID]) -> None:
    """One ``process_pending_embedding`` run per row, on fresh sessions."""
    from services import memory_service

    sessions = []

    def _get_db() -> AsyncIterator:
        async def _gen():
            session = worker.factory()
            sessions.append(session)
            yield session

        return _gen()

    try:
        with (
            patch("db.base.get_db", side_effect=_get_db),
            patch("services.embedding_service.EmbeddingService", MagicMock()),
            patch(
                "services.context_routing.resolve_context_routing",
                AsyncMock(return_value=("test-collection", embedding())),
            ),
        ):
            for memory_id in memory_ids:
                await memory_service.process_pending_embedding(memory_id)
    finally:
        for session in sessions:
            await session.close()


def upserted_points(qdrant: MagicMock) -> set[UUID]:
    return {
        UUID(str(point.id))
        for call in qdrant.upsert.await_args_list
        for point in call.kwargs["points"]
    }


async def test_erasure_cancel_requeue_rebuilds_every_canonical_row_and_stop_finalizes(
    managed, monkeypatch
):
    """A cancel after a spent vector attempt re-queues the creator's feed rows (#2304)."""
    from services.account_erasure_service import AccountErasureService

    await committed(managed, [product("one"), product("two")])
    await apply_pending(managed, monkeypatch)
    rows = await live_rows(managed)
    assert len(rows) == 2 and {r.embedding_status for r in rows} == {"success"}
    async with managed.factory() as db:
        creator = (await db.get(Context, ctx_of(managed))).created_by
        email = await db.scalar(select(User.email).where(User.user_id == creator))
        request = ErasureRequest(
            user_id=creator,
            user_email_hash=sha256_hex(email),
            initiated_by=creator,
            is_self_service=True,
            reason_code="self_service",
            status=STATUS_COOLING_OFF,
            requested_at=utcnow() - timedelta(days=8),
            confirmed_at=utcnow() - timedelta(days=7, minutes=1),
            scheduled_for=utcnow() + timedelta(hours=1),
            ip_address="203.0.113.9",
            user_agent="agent",
            # The vector-store step ran once (and may have deleted the points).
            deleted_data_summary={"retry": {"attempts": 1, "step": "vector_store"}},
        )
        db.add(request)
        await db.commit()
        request_id = request.id
    try:
        async with managed.factory() as db:
            cancelled = await AccountErasureService(db).cancel_self_service(user_id=creator)
            assert cancelled.status == STATUS_CANCELLED
        ids = [r.id for r in rows]
        assert {s[0] for s in (await statuses(managed, ids)).values()} == {"pending"}

        qdrant = stubs(monkeypatch)
        await run_pipeline(managed, ids)

        after = await statuses(managed, ids)
        assert {s[:2] for s in after.values()} == {("success", 0)}, after
        assert upserted_points(qdrant) == {r.summary_embedding_id for r in rows}
        await stop(managed)
        assert (await finalize(managed))["mode"] == "manual"
    finally:
        async with managed.factory() as db:
            await db.execute(delete(ErasureRequest).where(ErasureRequest.id == request_id))
            await db.commit()


async def test_admin_retry_returns_a_failed_feed_row_to_success(managed, monkeypatch):
    """The end state rows reached before the fix: failed at the ceiling, no point."""
    from api.routes.admin import retry_failed_embeddings

    await committed(managed, [product("one")])
    await apply_pending(managed, monkeypatch)
    (row,) = await live_rows(managed)
    async with managed.factory() as db:
        await db.execute(
            update(Memory)
            .where(Memory.id == row.id)
            .values(
                embedding_status="failed",
                embedding_retry_count=MAX_EMBEDDING_RETRIES,
                embedding_error="SYNC-LOCKED: This data is feed-managed.",
            )
        )
        await db.commit()
    stopped = await stop(managed)
    assert stopped["mode"] == "stopping"
    refused = await human(
        managed, "POST", "management/finalize", body={"operation_id": str(uuid4())}
    )
    assert refused.status_code == 409, refused.text

    async with managed.factory() as db:
        reset = await retry_failed_embeddings(
            user={"user_id": managed.store.users["admin"], "role": "admin"},
            db=db,
            context_id=ctx_of(managed),
            workspace_id=None,
            include_erased=False,
        )
    assert reset["reset_count"] == 1
    qdrant = stubs(monkeypatch)
    await run_pipeline(managed, [row.id])

    assert (await statuses(managed, [row.id]))[row.id] == ("success", 0, None)
    assert upserted_points(qdrant) == {row.summary_embedding_id}
    # Rebuilt in stopping mode too, so the stop can finish.
    assert (await finalize(managed))["mode"] == "manual"


async def test_a_failed_payload_patch_requeue_rebuilds_the_feed_row(managed, monkeypatch):
    """``RequeueEmbeddingOnFailure`` re-queues a row whose point patch failed."""
    from services.payload_sync import PlannedPatch, RequeueEmbeddingOnFailure

    await committed(managed, [product("one")])
    await apply_pending(managed, monkeypatch)
    (row,) = await live_rows(managed)
    policy = RequeueEmbeddingOnFailure("erasure_details_payload", reason="erasure_details_payload")
    async with managed.factory() as db:
        memory = await db.get(Memory, row.id)
        await policy.patch_failed(
            db, PlannedPatch(memory.id, memory, embedded=True), RuntimeError("store down")
        )
        await db.commit()
    assert policy.requeued_after_error == 1
    assert (await statuses(managed, [row.id]))[row.id][0] == "pending"

    qdrant = stubs(monkeypatch)
    await run_pipeline(managed, [row.id])

    assert (await statuses(managed, [row.id]))[row.id] == ("success", 0, None)
    assert upserted_points(qdrant) == {row.summary_embedding_id}


async def test_a_refused_managed_row_keeps_its_retry_budget(managed, monkeypatch):
    """A row behind its canonical record is refused, and never reaches the ceiling."""
    await committed(managed, [product("one")])
    await apply_pending(managed, monkeypatch)
    (row,) = await live_rows(managed)
    # A newer committed run moves the canonical record on; this row is now
    # the previous version, which only the indexer may replace.
    await committed(managed, [product("one", title="Next version")])
    async with managed.factory() as db:
        await db.execute(
            update(Memory).where(Memory.id == row.id).values(embedding_status="pending")
        )
        await db.commit()
    qdrant = stubs(monkeypatch)
    for _attempt in range(MAX_EMBEDDING_RETRIES + 1):
        await run_pipeline(managed, [row.id])
        status, retries, error = (await statuses(managed, [row.id]))[row.id]
        assert status == "failed"
        # Not spent: the sweep keeps re-probing it after the backoff.
        assert retries == 0
        assert error.startswith("SYNC-LOCKED: ")
        async with managed.factory() as db:
            await db.execute(
                update(Memory)
                .where(Memory.id == row.id)
                .values(embedding_attempted_at=utcnow() - timedelta(days=1))
            )
            await db.commit()
    qdrant.upsert.assert_not_awaited()
    # The indexer applies the new version; the stop then finishes.
    await apply_pending(managed, monkeypatch)
    await stop(managed)
    assert (await finalize(managed))["mode"] == "manual"


@pytest.mark.parametrize("change", ["edited", "in_hand", "tombstoned", "generic"])
async def test_only_a_restore_of_unchanged_canonical_content_passes(managed, monkeypatch, change):
    from services.vector_erasure import embedding_author_current

    await committed(managed, [product("one")])
    await apply_pending(managed, monkeypatch)
    (row,) = await live_rows(managed)
    qdrant = stubs(monkeypatch)
    async with managed.factory() as db:
        if change == "edited":
            await db.execute(
                update(Memory)
                .where(Memory.id == row.id)
                .values(content='{"title": "Edited outside the feed"}')
            )
        elif change == "tombstoned":
            await db.execute(
                update(SyncRecord)
                .where(SyncRecord.doc_id == row.resource_doc_id)
                .values(payload=None)
            )
        memory = await db.get(Memory, row.id)
        if change == "in_hand":
            memory.content = '{"title": "Changed in hand"}'
        with pytest.raises(ManagedDataLockedError):
            if change == "generic":
                await embedding_author_current(db, memory)
            else:
                await resource_indexer.ResourceIndexer(db).rebuild_point(
                    memory,
                    collection_name="test-collection",
                    embedding_service=embedding(),
                    canonical_rebuild=True,
                )
        await db.rollback()
    qdrant.upsert.assert_not_awaited()
    # Each change was rolled back: the unchanged row passes, and its point is written.
    async with managed.factory() as db:
        memory = await db.get(Memory, row.id)
        await resource_indexer.ResourceIndexer(db).rebuild_point(
            memory,
            collection_name="test-collection",
            embedding_service=embedding(),
            canonical_rebuild=True,
        )
        await db.commit()
    assert upserted_points(qdrant) == {row.summary_embedding_id}


async def test_the_canonical_rebuild_holds_the_fence_against_a_transition(managed, monkeypatch):
    """Two sessions: the test and the write share one fence a transition cannot cross."""
    from services.vector_erasure import embedding_author_current

    await committed(managed, [product("one")])
    await apply_pending(managed, monkeypatch)
    (row,) = await live_rows(managed)
    qdrant = stubs(monkeypatch)
    async with managed.factory() as rebuild:
        memory = await rebuild.get(Memory, row.id)
        assert await embedding_author_current(rebuild, memory, canonical_rebuild=True)
        # The rebuild's transaction holds the shared fence: no stop meanwhile.
        response = await human(
            managed, "POST", "management/stop", body={"operation_id": str(uuid4())}
        )
        assert response.status_code == 409, response.text
        await rebuild.rollback()
    async with managed.factory() as transition:
        await target_fence(transition, ctx_of(managed), exclusive=True)
        async with managed.factory() as rebuild:
            memory = await rebuild.get(Memory, row.id)
            with pytest.raises(ConflictError):
                await resource_indexer.ResourceIndexer(rebuild).rebuild_point(
                    memory,
                    collection_name="test-collection",
                    embedding_service=embedding(),
                    canonical_rebuild=True,
                )
            await rebuild.rollback()
        await transition.rollback()
    qdrant.upsert.assert_not_awaited()
    assert (await stop(managed))["mode"] == "stopping"
