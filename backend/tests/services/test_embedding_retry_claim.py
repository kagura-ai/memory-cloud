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

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

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
    # NULL-safe backoff: a failed row with NULL updated_at is still eligible
    # (never permanently stuck — the state #979 exists to prevent).
    assert "IS NULL" in sql
    # existing branches preserved
    assert "'PENDING'" in sql
    assert "'PROCESSING'" in sql


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
    assert "IS NULL" in sql  # NULL updated_at -> eligible, never stuck


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
    memory.user_id = "u1"
    memory.summary = "s"
    memory.workspace_id = uuid4()
    memory.context_id = uuid4()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=memory)
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
    db = _worker_db([_claimed(memory_id), _memory_row(memory_id), _rowcount(0)])
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

    # claim UPDATE, SELECT memory, success UPDATE — and nothing after a lost claim.
    assert len(captured) == 3
    sql = str(
        captured[2].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
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

    assert len(captured) == 3
    sql = str(
        captured[2].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    ).upper()
    assert "EMBEDDING_STATUS = 'PROCESSING'" in sql.replace("MEMORIES.", "")
    assert "'FAILED'" in sql
