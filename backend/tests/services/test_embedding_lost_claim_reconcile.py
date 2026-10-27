"""#2363: an embedding run that lost its claim after writing deletes its point
when the row's author changed meanwhile (the author was erased: the row now
carries the pseudonym), or when the row is gone.

``_reconcile_after_lost_claim`` first re-queues a ``success`` row as before
(#2061); only when that moved nothing does it read the row's author.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

COLLECTION = "kagura_memories"
RAW = "google-oauth2|raw"


def _rowcount(n: int) -> MagicMock:
    result = MagicMock()
    result.rowcount = n
    return result


def _author(user_id: str | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=user_id)
    return result


def _shared(flag: bool) -> MagicMock:
    result = MagicMock()
    result.scalar = MagicMock(return_value=flag)
    return result


def _db(results: list[Any]) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=results)
    db.commit = AsyncMock()
    return db


async def _reconcile(
    db: MagicMock,
    memory_id: UUID,
    point_id: UUID | None = None,
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
            point_id=point_id or memory_id,
            user_id=RAW,
            wrote_points=wrote_points,
        )
    return requeued, delete_points, delete_chunks, log


def _sql(db: MagicMock, i: int) -> str:
    from sqlalchemy.dialects import postgresql

    stmt = db.execute.await_args_list[i].args[0]
    return " ".join(
        str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        .upper()
        .split()
    )


@pytest.mark.asyncio
async def test_a_finished_newer_run_is_re_queued_and_no_point_is_deleted() -> None:
    memory_id = uuid4()
    db = _db([_rowcount(1)])

    requeued, delete_points, delete_chunks, _ = await _reconcile(db, memory_id)

    assert requeued is True
    assert db.execute.await_count == 1  # the re-queue, no read
    delete_points.assert_not_awaited()
    delete_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_erased_authors_row_loses_the_point_this_run_wrote() -> None:
    """The erasure's dequeue left the row ``failed`` under the pseudonym: the
    point goes, and the row stays out of the queue (the re-queue from
    ``processing`` matches nothing)."""
    memory_id = uuid4()
    db = _db([_rowcount(0), _author("pseudonym"), _rowcount(0)])

    requeued, delete_points, delete_chunks, log = await _reconcile(db, memory_id)

    assert requeued is False
    delete_points.assert_awaited_once_with([str(memory_id)], COLLECTION)
    delete_chunks.assert_awaited_once_with([memory_id], COLLECTION)
    log.info.assert_called_once_with(
        "embedding_points_removed_after_lost_claim",
        memory_id=str(memory_id),
        collection=COLLECTION,
        reason="author_changed",
    )
    # the read is by primary key and takes no lock
    assert _sql(db, 1) == (
        f"SELECT MEMORIES.USER_ID FROM MEMORIES WHERE MEMORIES.ID = '{memory_id}'".upper()
    )
    # then a re-queue that may also move a ``processing`` row (never ``failed``)
    requeue = _sql(db, 2)
    assert requeue.startswith("UPDATE MEMORIES SET EMBEDDING_STATUS='PENDING' WHERE")
    assert "'SUCCESS', 'PROCESSING'" in requeue


@pytest.mark.asyncio
async def test_a_newer_run_in_flight_under_the_new_author_is_fenced_and_re_queued() -> None:
    """The author changed and a newer run (under the new author) is still
    ``processing``: its point may have been replaced by this one, so the row
    goes back to ``pending`` and the sweep writes it again."""
    memory_id = uuid4()
    db = _db([_rowcount(0), _author("pseudonym"), _rowcount(1)])

    requeued, delete_points, _, _ = await _reconcile(db, memory_id)

    assert requeued is True
    delete_points.assert_awaited_once_with([str(memory_id)], COLLECTION)


@pytest.mark.asyncio
async def test_a_resource_row_loses_its_unshared_indexer_point_and_no_chunks() -> None:
    memory_id, point_id = uuid4(), uuid4()
    db = _db([_rowcount(0), _author("pseudonym"), _shared(False), _rowcount(0)])

    _, delete_points, delete_chunks, _ = await _reconcile(db, memory_id, point_id)

    delete_points.assert_awaited_once_with([str(point_id)], COLLECTION)
    delete_chunks.assert_not_awaited()  # a resource row has no chunk points


@pytest.mark.asyncio
@pytest.mark.parametrize("author", ["pseudonym", None])
async def test_a_resource_point_another_live_row_names_is_kept(author: str | None) -> None:
    """A resource point is the uuid5 of the document version: the same
    version indexed into another context sharing the collection is the other
    row's point too (``unshared_point_id``), erased author or gone row."""
    memory_id, point_id = uuid4(), uuid4()
    results = [_rowcount(0), _author(author), _shared(True)]
    if author is not None:
        results.append(_rowcount(0))
    db = _db(results)

    _, delete_points, delete_chunks, _ = await _reconcile(db, memory_id, point_id)

    delete_points.assert_not_awaited()
    delete_chunks.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_gone_row_loses_the_point_this_run_wrote() -> None:
    memory_id = uuid4()
    db = _db([_rowcount(0), _author(None)])

    requeued, delete_points, delete_chunks, log = await _reconcile(db, memory_id)

    assert requeued is False
    delete_points.assert_awaited_once_with([str(memory_id)], COLLECTION)
    delete_chunks.assert_awaited_once_with([memory_id], COLLECTION)
    assert log.info.call_args.kwargs["reason"] == "row_gone"
    assert db.execute.await_count == 2  # nothing to re-queue


@pytest.mark.asyncio
async def test_the_same_author_keeps_its_points() -> None:
    """A row re-queued by a switch or an edit (``pending``), a newer run in
    flight, a failure of its own: not this run's to clean up."""
    memory_id = uuid4()
    db = _db([_rowcount(0), _author(RAW)])

    requeued, delete_points, delete_chunks, log = await _reconcile(db, memory_id)

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
        db, memory_id, wrote_points=wrote_points, collection=collection
    )

    assert requeued is False
    db.execute.assert_not_awaited()
    delete_points.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_point_delete_is_logged_and_never_raised() -> None:
    memory_id = uuid4()
    db = _db([_rowcount(0), _author("pseudonym")])

    requeued, _, delete_chunks, log = await _reconcile(
        db,
        memory_id,
        delete_points=AsyncMock(side_effect=RuntimeError(f"store down: user {RAW}")),
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

    requeued, delete_points, _, log = await _reconcile(db, memory_id)

    assert requeued is False
    delete_points.assert_not_awaited()
    assert log.warning.call_args.args[0] == "embedding_points_removal_failed"
