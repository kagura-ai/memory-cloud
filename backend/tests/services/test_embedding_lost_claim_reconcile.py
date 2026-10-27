"""#2363: an embedding run that lost its claim after writing deletes its points
when the row's author was erased meanwhile, or when the row is gone.

``_reconcile_after_lost_claim`` first re-queues a ``success`` row as before
(#2061); only when that moved nothing does it read the row: the erased-author
marker or no row at all means the points this run wrote must not stay.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from config.constants import ERASED_AUTHOR_EMBEDDING_ERROR

COLLECTION = "kagura_memories"


def _rowcount(n: int) -> MagicMock:
    result = MagicMock()
    result.rowcount = n
    return result


def _row(embedding_error: str | None) -> MagicMock:
    result = MagicMock()
    result.one_or_none = MagicMock(return_value=MagicMock(embedding_error=embedding_error))
    return result


def _no_row() -> MagicMock:
    result = MagicMock()
    result.one_or_none = MagicMock(return_value=None)
    return result


def _named(*point_ids: UUID) -> MagicMock:
    result = MagicMock()
    result.scalars = MagicMock(return_value=list(point_ids))
    return result


def _db(results: list[Any]) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=results)
    db.commit = AsyncMock()
    return db


async def _reconcile(
    db: MagicMock,
    memory_id: UUID,
    point_ids: list[str],
    *,
    wrote_points: bool = True,
    collection: str | None = COLLECTION,
    delete_points: AsyncMock | None = None,
) -> tuple[bool, AsyncMock, AsyncMock, MagicMock]:
    from services import memory_service

    delete_points = delete_points or AsyncMock()
    delete_chunks = AsyncMock()
    with (
        patch("db.qdrant.delete_points_from_qdrant", delete_points),
        patch("db.qdrant.delete_chunk_points", delete_chunks),
        patch.object(memory_service, "logger") as log,
    ):
        requeued = await memory_service._reconcile_after_lost_claim(
            db,
            memory_id,
            collection=collection,
            point_ids=point_ids,
            wrote_points=wrote_points,
        )
    return requeued, delete_points, delete_chunks, log


@pytest.mark.asyncio
async def test_a_finished_newer_run_is_re_queued_and_no_point_is_deleted() -> None:
    memory_id = uuid4()
    db = _db([_rowcount(1)])

    requeued, delete_points, delete_chunks, _ = await _reconcile(db, memory_id, [str(memory_id)])

    assert requeued is True
    assert db.execute.await_count == 1  # the re-queue, no read
    delete_points.assert_not_awaited()
    delete_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_erased_authors_row_loses_the_points_this_run_wrote() -> None:
    memory_id = uuid4()
    db = _db([_rowcount(0), _row(ERASED_AUTHOR_EMBEDDING_ERROR)])

    requeued, delete_points, delete_chunks, log = await _reconcile(
        db, memory_id, [str(memory_id), str(memory_id)]
    )

    assert requeued is False
    delete_points.assert_awaited_once_with([str(memory_id)], COLLECTION)
    delete_chunks.assert_awaited_once_with([memory_id], COLLECTION)
    log.info.assert_called_once_with(
        "embedding_points_removed_after_lost_claim",
        memory_id=str(memory_id),
        collection=COLLECTION,
        reason="erased_author",
    )
    # the read is by primary key and takes no lock
    read = db.execute.await_args_list[1].args[0]
    assert "FOR UPDATE" not in str(read)
    assert read.compile().params == {"id_1": memory_id}


@pytest.mark.asyncio
async def test_an_erased_authors_resource_row_loses_the_indexer_point_too() -> None:
    memory_id, point_id = uuid4(), uuid4()
    db = _db([_rowcount(0), _row(ERASED_AUTHOR_EMBEDDING_ERROR)])

    _, delete_points, delete_chunks, _ = await _reconcile(
        db, memory_id, [str(memory_id), str(point_id)]
    )

    delete_points.assert_awaited_once_with([str(memory_id), str(point_id)], COLLECTION)
    delete_chunks.assert_awaited_once_with([memory_id], COLLECTION)
    assert db.execute.await_count == 2  # no "who names it" read for a live row


@pytest.mark.asyncio
async def test_a_gone_row_loses_the_points_this_run_wrote() -> None:
    memory_id = uuid4()
    db = _db([_rowcount(0), _no_row()])

    _, delete_points, delete_chunks, log = await _reconcile(db, memory_id, [str(memory_id)])

    delete_points.assert_awaited_once_with([str(memory_id)], COLLECTION)
    delete_chunks.assert_awaited_once_with([memory_id], COLLECTION)
    assert log.info.call_args.kwargs["reason"] == "row_gone"


@pytest.mark.asyncio
async def test_a_gone_resource_row_keeps_a_point_a_live_row_names() -> None:
    """A resource point is the uuid5 of the document version: a row indexed
    again after the deleted one names the same point, and it stays."""
    memory_id, shared, own_indexer = uuid4(), uuid4(), uuid4()
    db = _db([_rowcount(0), _no_row(), _named(shared)])

    _, delete_points, _, _ = await _reconcile(
        db, memory_id, [str(memory_id), str(shared), str(own_indexer)]
    )

    delete_points.assert_awaited_once_with([str(memory_id), str(own_indexer)], COLLECTION)
    named = db.execute.await_args_list[2].args[0].compile().params
    assert set(named["summary_embedding_id_1"]) == {shared, own_indexer}


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "claim_expired: x", "QUOTA-002: capped"])
async def test_any_other_row_keeps_its_points(error: str | None) -> None:
    """A row re-queued by a switch or an edit (``pending``), a newer run in
    flight, a failure of its own: not this run's to clean up."""
    memory_id = uuid4()
    db = _db([_rowcount(0), _row(error)])

    requeued, delete_points, delete_chunks, log = await _reconcile(db, memory_id, [str(memory_id)])

    assert requeued is False
    delete_points.assert_not_awaited()
    delete_chunks.assert_not_awaited()
    log.info.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("wrote_points", "collection"), [(False, COLLECTION), (True, None)])
async def test_a_run_that_wrote_nothing_reads_and_deletes_nothing(
    wrote_points: bool, collection: str | None
) -> None:
    memory_id = uuid4()
    db = _db([])

    requeued, delete_points, _, _ = await _reconcile(
        db, memory_id, [str(memory_id)], wrote_points=wrote_points, collection=collection
    )

    assert requeued is False
    db.execute.assert_not_awaited()
    delete_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_point_delete_is_logged_and_never_raised() -> None:
    memory_id = uuid4()
    db = _db([_rowcount(0), _row(ERASED_AUTHOR_EMBEDDING_ERROR)])

    requeued, _, delete_chunks, log = await _reconcile(
        db,
        memory_id,
        [str(memory_id)],
        delete_points=AsyncMock(side_effect=RuntimeError("store down: user u-raw")),
    )

    assert requeued is False
    delete_chunks.assert_not_awaited()
    log.warning.assert_called_once_with(
        "embedding_points_removal_failed",
        memory_id=str(memory_id),
        collection=COLLECTION,
        error_class="RuntimeError",
    )
    log.info.assert_not_called()


@pytest.mark.asyncio
async def test_a_failed_read_is_logged_and_never_raised() -> None:
    memory_id = uuid4()
    db = _db([_rowcount(0), RuntimeError("db")])

    requeued, delete_points, _, log = await _reconcile(db, memory_id, [str(memory_id)])

    assert requeued is False
    delete_points.assert_not_awaited()
    assert log.warning.call_args.args[0] == "embedding_points_removal_failed"
