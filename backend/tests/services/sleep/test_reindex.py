"""Tests for Sleep Maintenance Phase 5: Re-index.

Issue #101: Re-embed changed memories and upsert to Qdrant.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.reindex import ReindexPhase


@pytest.fixture
def mock_db():
    db = AsyncMock()
    return db


@pytest.fixture
def reindex_phase(mock_db):
    with patch("services.sleep.reindex.EmbeddingService"):
        phase = ReindexPhase(mock_db)
        phase.embedding_service = AsyncMock()
        return phase


def _make_memory(memory_id=None, summary="test summary", importance=0.7):
    """Create a mock Memory object."""
    m = MagicMock()
    m.id = memory_id or uuid4()
    m.summary = summary
    m.context_summary = "test context"
    m.type = "note"
    m.importance = importance
    m.scope = "persistent"
    m.tags = ["test"]
    m.created_at = MagicMock()
    m.created_at.isoformat.return_value = "2026-01-01T00:00:00"
    m.updated_at = MagicMock()
    m.updated_at.isoformat.return_value = "2026-01-02T00:00:00"
    m.deleted_at = None
    return m


def _mock_batch_fetch(mock_db, memories):
    """Mock the batch SELECT ... WHERE id IN (...) pattern."""
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = memories
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    mock_db.execute = AsyncMock(return_value=mock_result)


class TestReindexPhase:
    """Test ReindexPhase execution."""

    @pytest.mark.asyncio
    async def test_empty_set_returns_early(self, reindex_phase):
        """No work when changed_memory_ids is empty."""
        result = await reindex_phase.execute(
            changed_memory_ids=set(),
            user_id="user-1",
        )
        assert result.phase_name == "reindex"
        assert result.success is True
        assert result.memories_processed == 0
        assert result.details["message"] == "no_memories_to_reindex"

    @pytest.mark.asyncio
    @patch("services.sleep.reindex.add_memory_to_qdrant", new_callable=AsyncMock)
    async def test_successful_reindex(self, mock_qdrant, reindex_phase, mock_db):
        """Successfully re-embed and upsert memories."""
        mem1 = _make_memory()
        mem2 = _make_memory()
        changed_ids = {mem1.id, mem2.id}

        _mock_batch_fetch(mock_db, [mem1, mem2])
        # #471: reindex now calls embed_with_usage() returning (vector, tokens).
        reindex_phase.embedding_service.embed_with_usage = AsyncMock(return_value=([0.1] * 768, 50))

        result = await reindex_phase.execute(
            changed_memory_ids=changed_ids,
            user_id="user-1",
            workspace_id="ws-1",
            context_id="ctx-1",
        )

        assert result.success is True
        assert result.memories_processed == 2
        assert result.embedding_calls_used == 2
        assert result.details["reindexed"] == 2
        assert result.details["failed"] == 0
        assert mock_qdrant.call_count == 2

    @pytest.mark.asyncio
    @patch("services.sleep.reindex.add_memory_to_qdrant", new_callable=AsyncMock)
    async def test_skips_deleted_memories(self, mock_qdrant, reindex_phase, mock_db):
        """Deleted memories (not in batch result) are skipped."""
        changed_ids = {uuid4()}

        _mock_batch_fetch(mock_db, [])  # No memories returned

        result = await reindex_phase.execute(
            changed_memory_ids=changed_ids,
            user_id="user-1",
        )

        assert result.success is True
        assert result.memories_processed == 0
        mock_qdrant.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.sleep.reindex.add_memory_to_qdrant", new_callable=AsyncMock)
    async def test_partial_failure(self, mock_qdrant, reindex_phase, mock_db):
        """One failure doesn't stop processing other memories."""
        mem_ok = _make_memory()
        mem_fail = _make_memory()
        changed_ids = {mem_ok.id, mem_fail.id}

        _mock_batch_fetch(mock_db, [mem_fail, mem_ok])

        # First embed fails, second succeeds
        reindex_phase.embedding_service.embed_with_usage = AsyncMock(
            side_effect=[RuntimeError("embed failed"), ([0.1] * 768, 50)]
        )

        result = await reindex_phase.execute(
            changed_memory_ids=changed_ids,
            user_id="user-1",
            workspace_id="ws-1",
            context_id="ctx-1",
        )

        assert result.success is True  # Partial success
        assert result.details["reindexed"] == 1
        assert result.details["failed"] == 1

    @pytest.mark.asyncio
    @patch("services.sleep.reindex.add_memory_to_qdrant", new_callable=AsyncMock)
    async def test_all_fail_marks_unsuccessful(self, mock_qdrant, reindex_phase, mock_db):
        """All failures marks phase as unsuccessful."""
        mem = _make_memory()
        changed_ids = {mem.id}

        _mock_batch_fetch(mock_db, [mem])

        reindex_phase.embedding_service.embed_with_usage = AsyncMock(
            side_effect=RuntimeError("embed failed")
        )

        result = await reindex_phase.execute(
            changed_memory_ids=changed_ids,
            user_id="user-1",
        )

        assert result.success is False
        assert "All" in result.error
