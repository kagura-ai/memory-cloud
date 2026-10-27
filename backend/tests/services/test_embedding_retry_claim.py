"""Tests for process_pending_embedding's failed-row claim gate (#979).

The claim is the authoritative, atomic gate that decides whether a memory is
(re)processed and increments the retry counter. Exercising the full embedding
pipeline needs a real DB + Qdrant + embedding provider, so here we make the
claim "not claimed" (returning None) to force the early return after the claim,
then statically assert the compiled claim statement carries the #979 gate:
``failed AND embedding_retry_count < MAX`` plus a CASE that increments the
counter only for previously-failed rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql


@pytest.mark.asyncio
async def test_claim_gate_includes_failed_with_retry_budget_and_increments():
    from services import memory_service

    captured: list = []
    claim_result = MagicMock()
    claim_result.scalar_one_or_none = MagicMock(return_value=None)  # not claimed -> early return

    db = MagicMock()

    async def _execute(stmt, *_a, **_k):
        captured.append(stmt)
        return claim_result

    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()

    async def _aiter():
        yield db

    with patch("db.base.get_db", return_value=_aiter()):
        await memory_service.process_pending_embedding(uuid4())

    # Only the claim UPDATE ran (claim returned None -> early return).
    assert len(captured) == 1
    sql = str(
        captured[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).upper()

    # The claim is widened to failed rows under the retry gate...
    assert "'FAILED'" in sql
    assert "EMBEDDING_RETRY_COUNT" in sql
    assert "< 3" in sql  # MAX_EMBEDDING_RETRIES default
    # ...and increments the counter via a CASE (only for failed rows).
    assert "CASE" in sql
    # NULL-safe backoff: a failed row with a NULL clock is still eligible
    # (never permanently stuck — the state #979 exists to prevent).
    assert "IS NULL" in sql
    # #1852: the pipeline keeps its own clock; the claim must not stamp the
    # user-visible updated_at, which list / changes_since read as an edit.
    assert "EMBEDDING_ATTEMPTED_AT" in sql
    assert "UPDATED_AT" not in sql
    # existing branches preserved
    assert "'PENDING'" in sql
    assert "'PROCESSING'" in sql


@pytest.mark.asyncio
async def test_a_stale_reclaim_is_bounded_by_and_spends_the_retry_budget():
    """#2095: a run that outlives its claim every time is reclaimed at most
    MAX_EMBEDDING_RETRIES times. The processing branch carries the budget
    bound and the CASE counts a reclaim; a pending claim still spends nothing."""
    from services import memory_service

    captured: list = []
    claim_result = MagicMock()
    claim_result.scalar_one_or_none = MagicMock(return_value=None)
    db = MagicMock()

    async def _execute(stmt, *_a, **_k):
        captured.append(stmt)
        return claim_result

    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()

    async def _aiter():
        yield db

    with patch("db.base.get_db", return_value=_aiter()):
        await memory_service.process_pending_embedding(uuid4())

    sql = _compiled(captured[0])
    set_clause, where = sql.split(" WHERE ", 1)
    processing_branch = where.split("EMBEDDING_STATUS = 'PROCESSING'", 1)[1].split(
        "EMBEDDING_STATUS = 'FAILED'"
    )[0]
    assert "EMBEDDING_RETRY_COUNT < 3" in processing_branch
    assert "CASE WHEN (EMBEDDING_STATUS IN ('FAILED', 'PROCESSING'))" in set_clause
    assert "THEN EMBEDDING_RETRY_COUNT + 1 ELSE EMBEDDING_RETRY_COUNT END" in set_clause


def test_reclaim_clauses_split_on_the_budget():
    """The claim/sweep reclaim clause and the sweep's exhaust clause cover the
    same stale processing rows, split exactly at MAX_EMBEDDING_RETRIES."""
    from datetime import UTC

    from services.memory_service import (
        embedding_reclaim_eligible_clause,
        embedding_reclaim_exhausted_clause,
    )

    cutoff = datetime(2026, 6, 15, tzinfo=UTC)
    eligible = _compiled(embedding_reclaim_eligible_clause(cutoff))
    exhausted = _compiled(embedding_reclaim_exhausted_clause(cutoff))
    for sql in (eligible, exhausted):
        assert "EMBEDDING_STATUS = 'PROCESSING'" in sql
        assert "EMBEDDING_ATTEMPTED_AT IS NULL" in sql
        assert "EMBEDDING_ATTEMPTED_AT < '2026-06-15" in sql
    assert "EMBEDDING_RETRY_COUNT < 3" in eligible
    assert "EMBEDDING_RETRY_COUNT >= 3" in exhausted


def test_retry_eligibility_clause_is_bounded_and_null_safe():
    """The shared clause (used by both the claim and the sweep prefilter) gates
    on retry budget and is NULL-safe on the backoff timestamp."""
    from datetime import UTC, datetime

    from services.memory_service import embedding_retry_eligible_clause

    clause = embedding_retry_eligible_clause(datetime(2026, 6, 15, tzinfo=UTC))
    sql = str(
        clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).upper()
    assert "'FAILED'" in sql
    assert "EMBEDDING_RETRY_COUNT < 3" in sql  # bounded by MAX_EMBEDDING_RETRIES
    assert "IS NULL" in sql  # NULL clock -> eligible, never stuck
    assert "EMBEDDING_ATTEMPTED_AT" in sql and "UPDATED_AT" not in sql  # #1852


@pytest.mark.asyncio
async def test_claim_returns_early_when_not_claimable():
    """A row that the claim WHERE doesn't match (exhausted budget, wrong
    status, soft-deleted, or already claimed) yields scalar None -> the
    function returns without touching the embedding pipeline (no commit)."""
    from services import memory_service

    claim_result = MagicMock()
    claim_result.scalar_one_or_none = MagicMock(return_value=None)

    db = MagicMock()
    db.execute = AsyncMock(return_value=claim_result)
    db.commit = AsyncMock()

    async def _aiter():
        yield db

    with patch("db.base.get_db", return_value=_aiter()):
        await memory_service.process_pending_embedding(uuid4())

    db.commit.assert_not_awaited()  # never got past the claim


# --------------------------------------------------------------------- #1525
# An embedding-model migration's switch re-queues rows to `pending` after
# flipping routing. A worker that claimed a row before the flip embedded it
# under the OLD routing; its completion write must not overwrite the re-queue.


def _worker_db(execute_results):
    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    db.execute = AsyncMock(side_effect=list(execute_results))
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _claimed(memory_id):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=memory_id)
    return result


def _memory_row(memory_id):
    memory = MagicMock()
    memory.id = memory_id
    # the summary point is the row id (a resource row's is the indexer's)
    memory.point_id = memory_id
    memory.embedding_suppressed_at = None
    memory.embedding_erasure_restored_at = None
    memory.user_id = "u1"
    memory.summary = "s"
    # #2092: the run digests summary + content before it embeds; NULL = the
    # row was never embedded under the digest column (a first embed).
    memory.content = "c"
    memory.embedded_text_digest = None
    memory.workspace_id = uuid4()
    memory.context_id = uuid4()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=memory)
    return result


def _chunk_flag(enabled):
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=enabled)
    return result


def _rowcount(n):
    result = MagicMock()
    result.rowcount = n
    result.scalar_one_or_none = MagicMock(return_value=None if n == 0 else 0)
    return result


@pytest.mark.asyncio
async def test_success_write_is_fenced_on_still_owning_the_claim():
    from services import memory_service

    memory_id = uuid4()
    captured: list = []
    # #2015: the content-chunk flag is read after the summary point (off here).
    # #2061: a lost claim re-queues the row (fenced on success; nothing to move here).
    db = _worker_db(
        [
            _claimed(memory_id),
            _memory_row(memory_id),
            _chunk_flag(False),
            _rowcount(0),
            _rowcount(0),
            _reconcile_read("u1"),
        ]
    )
    original_execute = db.execute

    async def _execute(stmt, *a, **k):
        captured.append(stmt)
        return await original_execute(stmt, *a, **k)

    db.execute = AsyncMock(side_effect=_execute)

    async def _aiter():
        yield db

    embed_svc = MagicMock()
    embed_svc.embed = AsyncMock(return_value=[0.1])
    embed_svc.model = "m"

    with (
        patch("db.base.get_db", return_value=_aiter()),
        patch("services.embedding_service.EmbeddingService", MagicMock()),
        patch(
            "services.context_routing.resolve_context_routing",
            AsyncMock(return_value=("kagura_memories", embed_svc)),
        ),
        patch.object(memory_service, "add_memory_to_qdrant", AsyncMock()),
        patch.object(memory_service, "build_memory_point", return_value=({}, [], [])),
        patch.object(memory_service, "_create_knn_seed_edges", AsyncMock()) as knn,
        patch.object(memory_service, "_create_tag_cooccurrence_seed_edges", AsyncMock()) as tags,
    ):
        await memory_service.process_pending_embedding(memory_id)

    # claim UPDATE, SELECT memory, chunk-flag SELECT, success UPDATE, the
    # re-queue (#2061), the read of the row's author (#2363: the same one,
    # the row is not gone) — and nothing else after a lost claim.
    assert len(captured) == 6
    sql = str(
        captured[3].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).upper()
    assert "EMBEDDING_STATUS = 'PROCESSING'" in sql.replace("MEMORIES.", "")
    assert "'SUCCESS'" in sql
    knn.assert_not_awaited()
    tags.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_write_is_fenced_on_still_owning_the_claim():
    from services import memory_service

    memory_id = uuid4()
    captured: list = []
    db = _worker_db([_claimed(memory_id), _memory_row(memory_id), _rowcount(0)])
    original_execute = db.execute

    async def _execute(stmt, *a, **k):
        captured.append(stmt)
        return await original_execute(stmt, *a, **k)

    db.execute = AsyncMock(side_effect=_execute)

    async def _aiter():
        yield db

    embed_svc = MagicMock()
    embed_svc.embed = AsyncMock(side_effect=RuntimeError("provider down"))

    with (
        patch("db.base.get_db", return_value=_aiter()),
        patch("services.embedding_service.EmbeddingService", MagicMock()),
        patch(
            "services.context_routing.resolve_context_routing",
            AsyncMock(return_value=("kagura_memories", embed_svc)),
        ),
    ):
        await memory_service.process_pending_embedding(memory_id)

    # claim, SELECT, failure UPDATE — no re-queue: the run failed before it
    # wrote a point (#2061).
    assert len(captured) == 3
    sql = str(
        captured[2].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).upper()
    assert "EMBEDDING_STATUS = 'PROCESSING'" in sql.replace("MEMORIES.", "")
    assert "'FAILED'" in sql


# --------------------------------------------------------------------- #2041
# A run that outlives the 60 s stale cutoff is reclaimed by the sweep: the
# newer run stamps its own `embedding_attempted_at` and the status stays
# `processing`. The old run's outcome must not land on the newer claim — a
# `success` there would hide a later failed chunk write of the newer run from
# every retry. Both writes are fenced on the stamp this run's claim wrote.

_CLAIM_STAMP = datetime(2026, 1, 2, 3, 4, 5, 678901)


def _compiled(stmt: Any) -> str:
    return (
        str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        .upper()
        .replace("MEMORIES.", "")
    )


async def _run_worker(
    memory_service: ModuleType,
    db: MagicMock,
    embed_svc: MagicMock,
    clock: Any = None,
) -> tuple[AsyncMock, AsyncMock, MagicMock]:
    async def _aiter():
        yield db

    with (
        patch("db.base.get_db", return_value=_aiter()),
        patch("utils.datetime.utcnow", side_effect=clock or (lambda: _CLAIM_STAMP)),
        patch("services.embedding_service.EmbeddingService", MagicMock()),
        patch(
            "services.context_routing.resolve_context_routing",
            AsyncMock(return_value=("kagura_memories", embed_svc)),
        ),
        patch.object(memory_service, "add_memory_to_qdrant", AsyncMock()),
        patch.object(memory_service, "build_memory_point", return_value=({}, [], [])),
        patch.object(memory_service, "_create_knn_seed_edges", AsyncMock()) as knn,
        patch.object(memory_service, "_create_tag_cooccurrence_seed_edges", AsyncMock()) as tags,
        patch.object(memory_service, "logger") as log,
    ):
        await memory_service.process_pending_embedding(db.memory_id)
    return knn, tags, log


def _capturing_db(memory_id: UUID, execute_results: list[MagicMock]) -> MagicMock:
    db = _worker_db(execute_results)
    db.memory_id = memory_id
    db.captured = []
    original_execute = db.execute

    async def _execute(stmt, *a, **k):
        db.captured.append(stmt)
        return await original_execute(stmt, *a, **k)

    db.execute = AsyncMock(side_effect=_execute)
    return db


@pytest.mark.asyncio
async def test_a_reclaimed_run_does_not_stamp_success_on_the_newer_claim():
    from services import memory_service

    memory_id = uuid4()
    # The newer claim moved the stamp: the fenced success UPDATE matches nothing.
    # The newer run is done (success), so the row is re-queued (#2061).
    db = _capturing_db(
        memory_id,
        [
            _claimed(memory_id),
            _memory_row(memory_id),
            _chunk_flag(False),
            _rowcount(0),
            _rowcount(1),
        ],
    )
    embed_svc = MagicMock()
    embed_svc.embed = AsyncMock(return_value=[0.1])
    embed_svc.model = "m"

    knn, tags, log = await _run_worker(memory_service, db, embed_svc)

    claim_sql, success_sql = _compiled(db.captured[0]), _compiled(db.captured[3])
    # The claim writes the stamp the outcome is fenced on.
    assert "EMBEDDING_ATTEMPTED_AT='2026-01-02 03:04:05.678901'" in claim_sql.split("WHERE", 1)[0]
    assert "'SUCCESS'" in success_sql
    where = success_sql.split("WHERE", 1)[1]
    assert "EMBEDDING_STATUS = 'PROCESSING'" in where
    assert "EMBEDDING_ATTEMPTED_AT = '2026-01-02 03:04:05.678901'" in where
    # Fenced out: the re-queue, one info line, nothing else (no seeding).
    assert len(db.captured) == 5
    _assert_requeue(db.captured[4])
    knn.assert_not_awaited()
    tags.assert_not_awaited()
    events = [c.args[0] for c in log.info.call_args_list]
    assert events == ["embedding_claim_lost"]
    assert log.info.call_args.kwargs["requeued"] is True
    # #2070: lost while the stamp was fresh — a re-queue, not a reclaim.
    assert log.info.call_args.kwargs["reason"] == "requeued"


@pytest.mark.asyncio
async def test_a_claim_lost_after_its_stamp_went_stale_is_logged_as_stale():
    """#2070: a run noticing the loss past the stale cutoff may have been
    reclaimed (failed renewals, a hung provider) — told apart from a re-queue."""
    from services import memory_service

    memory_id = uuid4()
    db = _capturing_db(memory_id, [_claimed(memory_id), _memory_row(memory_id), _rowcount(0)])
    embed_svc = MagicMock()
    embed_svc.embed = AsyncMock(side_effect=RuntimeError("provider hung"))
    times = iter([_CLAIM_STAMP])

    def _clock() -> datetime:
        return next(times, _CLAIM_STAMP + timedelta(seconds=61))

    _, _, log = await _run_worker(memory_service, db, embed_svc, clock=_clock)

    assert log.info.call_args.args[0] == "embedding_claim_lost"
    assert log.info.call_args.kwargs["reason"] == "stale"


@pytest.mark.asyncio
async def test_a_reclaimed_run_does_not_mark_the_newer_claim_failed():
    from services import memory_service

    memory_id = uuid4()
    db = _capturing_db(memory_id, [_claimed(memory_id), _memory_row(memory_id), _rowcount(0)])
    embed_svc = MagicMock()
    embed_svc.embed = AsyncMock(side_effect=RuntimeError("provider down"))

    _, _, log = await _run_worker(memory_service, db, embed_svc)

    failure_sql = _compiled(db.captured[2])
    assert "'FAILED'" in failure_sql
    where = failure_sql.split("WHERE", 1)[1]
    assert "EMBEDDING_STATUS = 'PROCESSING'" in where
    # The failure write itself moves the stamp (the #979 backoff clock); the
    # fence still compares the value this run's claim wrote.
    assert "EMBEDDING_ATTEMPTED_AT = '2026-01-02 03:04:05.678901'" in where
    # It failed before writing a point, so it replaced nothing: no re-queue (#2061).
    assert len(db.captured) == 3
    events = [c.args[0] for c in log.info.call_args_list]
    assert events == ["embedding_claim_lost"]
    assert log.info.call_args.kwargs["requeued"] is False
    log.warning.assert_not_called()
    log.error.assert_not_called()


@pytest.mark.asyncio
async def test_a_run_that_failed_after_writing_re_queues_a_finished_newer_run():
    """#2061: the summary point went out before the chunk write failed, so it
    may have replaced the newer run's point; the fenced re-queue follows."""
    from services import memory_service

    memory_id = uuid4()
    db = _capturing_db(
        memory_id,
        [
            _claimed(memory_id),
            _memory_row(memory_id),
            RuntimeError("chunk flag lookup"),  # the chunk write fails
            _rowcount(0),  # the fenced failure write: claim lost
            _rowcount(0),  # the re-queue: the newer run is still in flight
            _reconcile_read("u1"),  # #2363: the author the run wrote under
        ],
    )
    embed_svc = MagicMock()
    embed_svc.embed = AsyncMock(return_value=[0.1])

    _, _, log = await _run_worker(memory_service, db, embed_svc)

    assert len(db.captured) == 6
    _assert_requeue(db.captured[4])
    assert log.info.call_args.args[0] == "embedding_claim_lost"
    assert log.info.call_args.kwargs["requeued"] is False


def _assert_requeue(stmt: Any) -> None:
    """#2061: the lost claim's re-queue moves only a live ``success`` row to
    ``pending`` (a newer run still in flight, or an edit, is left alone)."""
    sql = _compiled(stmt)
    set_clause, where = sql.split(" WHERE ", 1)
    assert set_clause.endswith("SET EMBEDDING_STATUS='PENDING'")
    assert "EMBEDDING_STATUS = 'SUCCESS'" in where
    assert "DELETED_AT IS NULL" in where


@pytest.mark.asyncio
async def test_a_failed_re_queue_after_a_lost_claim_is_only_logged():
    from services import memory_service

    memory_id = uuid4()
    db = _capturing_db(
        memory_id,
        [
            _claimed(memory_id),
            _memory_row(memory_id),
            RuntimeError("chunk flag lookup"),
            _rowcount(0),
            RuntimeError("db"),
        ],
    )
    embed_svc = MagicMock()
    embed_svc.embed = AsyncMock(return_value=[0.1])

    _, _, log = await _run_worker(memory_service, db, embed_svc)

    assert log.info.call_args.kwargs["requeued"] is False
    warning = log.warning.call_args
    assert warning.args[0] == "embedding_requeue_failed"
    assert warning.kwargs["error_class"] == "RuntimeError"


def _reconcile_read(author: str | None) -> MagicMock:
    """#2363: the lost-claim reconcile's read of the row's author (``None``: no row)."""
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=author)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["success", "failure"])
async def test_a_run_that_lost_its_claim_to_an_erasure_deletes_its_points(path: str):
    """#2363: the erasure pseudonymized the row and marked it ``failed`` after
    deleting the author's points; this run's point (raw sub) went out before,
    so its outcome write matches nothing and it deletes what it wrote — on
    the success path and on a failure after the write alike. The marked row
    is not re-queued."""
    from services import memory_service

    memory_id = uuid4()
    after_write = (
        [_chunk_flag(False), _rowcount(0)]  # the fenced success write: claim lost
        if path == "success"
        else [RuntimeError("chunk flag lookup"), _rowcount(0)]  # the failure write: lost
    )
    db = _capturing_db(
        memory_id,
        [
            _claimed(memory_id),
            _memory_row(memory_id),
            *after_write,
            _rowcount(0),  # the re-queue moves nothing: the row is ``failed``
            _reconcile_read("pseudonym"),
            _rowcount(0),  # the re-queue from processing: nothing, still ``failed``
        ],
    )
    embed_svc = MagicMock()
    embed_svc.embed = AsyncMock(return_value=[0.1])
    embed_svc.model = "m"
    delete_points, delete_chunks = AsyncMock(), AsyncMock()

    with (
        patch("db.qdrant.delete_points_from_qdrant", delete_points),
        patch("db.qdrant.delete_chunk_points", delete_chunks),
    ):
        knn, tags, log = await _run_worker(memory_service, db, embed_svc)

    delete_points.assert_awaited_once_with([str(memory_id)], "kagura_memories")
    delete_chunks.assert_awaited_once_with([memory_id], "kagura_memories")
    events = [c.args[0] for c in log.info.call_args_list]
    assert events == ["embedding_points_removed_after_lost_claim", "embedding_claim_lost"]
    assert log.info.call_args_list[0].kwargs["reason"] == "author_changed"
    assert log.info.call_args.kwargs["requeued"] is False
    knn.assert_not_awaited()
    tags.assert_not_awaited()
