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
