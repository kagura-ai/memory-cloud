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
    # #2053: build_memory_point reads these as real values.
    m.user_id = "user-1"
    m.content = "test content body"
    m.client = "claude-code"
    m.context = None
    m.location_lat = None
    m.location_lon = None
    # Not a resource-indexer row (owns_resource_point is False).
    m.resource_id = None
    return m


def _make_resource_memory():
    """#2053: a row the resource indexer wrote; its point is ``point_id``."""
    from services.resource_indexer import resource_point_id

    m = _make_memory(summary="[resource] doc-1 v2", importance=0.6)
    m.content = '{"title": "Annual report"}'
    m.resource_id = "reports"
    m.resource_doc_id = "doc-1"
    m.resource_version = 2
    m.summary_embedding_id = resource_point_id("reports", "doc-1", 2)
    m.point_id = m.summary_embedding_id
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


class TestReindexContentChunks:
    """#2015: a reindexed memory's chunk points are rewritten too."""

    @pytest.mark.asyncio
    @patch("services.sleep.reindex.add_memory_to_qdrant", new_callable=AsyncMock)
    @patch("services.sleep.reindex.content_chunks_enabled", new_callable=AsyncMock)
    @patch("services.sleep.reindex.write_content_chunks", new_callable=AsyncMock)
    async def test_chunks_rewritten_with_the_flag_read_once(
        self, mock_write, mock_flag, mock_qdrant, reindex_phase, mock_db
    ):
        mock_flag.return_value = True
        mem1, mem2 = _make_memory(), _make_memory()
        _mock_batch_fetch(mock_db, [mem1, mem2])
        reindex_phase.embedding_service.embed_with_usage = AsyncMock(return_value=([0.1], 1))

        await reindex_phase.execute(
            changed_memory_ids={mem1.id, mem2.id},
            user_id="user-1",
            workspace_id=str(uuid4()),
            context_id=str(uuid4()),
        )

        mock_flag.assert_awaited_once()
        assert mock_write.await_count == 2
        kwargs = mock_write.await_args.kwargs
        assert kwargs["chunks_enabled"] is True
        assert kwargs["embed_svc"] is reindex_phase.embedding_service
        assert kwargs["base_payload"]["summary"] == "test summary"


class TestReindexPointShape:
    """#2053: the reindexed point is the same point the normal write path builds."""

    @pytest.mark.asyncio
    @patch("services.sleep.reindex.add_memory_to_qdrant", new_callable=AsyncMock)
    @patch("services.sleep.reindex.write_content_chunks", new_callable=AsyncMock)
    async def test_upsert_carries_bm25_sparse_and_canonical_payload(
        self, mock_write, mock_qdrant, reindex_phase, mock_db
    ):
        from services.memory_service import build_memory_point

        mem = _make_memory(summary="Qdrant sparse vector survives Sleep reindex")
        mem.context = {"issue": 2053}
        mem.location_lat = 35.68
        mem.location_lon = 139.76
        _mock_batch_fetch(mock_db, [mem])
        reindex_phase.embedding_service.embed_with_usage = AsyncMock(return_value=([0.1], 7))

        result = await reindex_phase.execute(
            changed_memory_ids={mem.id},
            user_id="user-1",
            workspace_id=str(uuid4()),
            context_id=str(uuid4()),
        )

        assert result.details["reindexed"] == 1
        # Dense embedding is unchanged: raw summary, usage still accounted.
        call = reindex_phase.embedding_service.embed_with_usage.await_args
        assert call.args[0] == "Qdrant sparse vector survives Sleep reindex"
        assert result.embedding_tokens == 7

        kwargs = mock_qdrant.await_args.kwargs
        expected_payload, expected_idx, expected_vals = build_memory_point(mem)
        assert kwargs["sparse_indices"]
        assert kwargs["sparse_indices"] == expected_idx
        assert kwargs["sparse_values"] == expected_vals
        # add_memory_to_qdrant (mocked here) adds the isolation fields itself.
        assert set(kwargs["payload"]) == set(expected_payload)
        assert kwargs["payload"]["location"] == {"lat": 35.68, "lon": 139.76}
        assert kwargs["payload"]["context"] == {"issue": 2053}
        # The chunk writer copies the very same payload.
        assert mock_write.await_args.kwargs["base_payload"] is kwargs["payload"]


class TestReindexResourceRows:
    """#2053: a resource-indexer row gets the indexer's point, not a stray one."""

    @pytest.mark.asyncio
    @patch("services.sleep.reindex.add_memory_to_qdrant", new_callable=AsyncMock)
    @patch("services.sleep.reindex.write_content_chunks", new_callable=AsyncMock)
    @patch("services.sleep.reindex.ResourceIndexer")
    async def test_resource_row_is_rebuilt_by_the_indexer(
        self, mock_indexer_cls, mock_write, mock_qdrant, reindex_phase, mock_db
    ):
        resource, other, plain = _make_resource_memory(), _make_resource_memory(), _make_memory()
        _mock_batch_fetch(mock_db, [resource, other, plain])

        async def _rebuild(memory, *, collection_name, embedding_service, usage):
            usage["tokens"] = 11
            return memory.point_id

        rebuild = AsyncMock(side_effect=_rebuild)
        mock_indexer_cls.return_value.rebuild_point = rebuild
        reindex_phase.embedding_service.embed_with_usage = AsyncMock(return_value=([0.1], 5))

        result = await reindex_phase.execute(
            changed_memory_ids={resource.id, other.id, plain.id},
            user_id="user-1",
            workspace_id=str(uuid4()),
            context_id=str(uuid4()),
        )

        # One indexer for the batch, one rebuild per resource row.
        mock_indexer_cls.assert_called_once_with(mock_db)
        assert {c.args[0] for c in rebuild.await_args_list} == {resource, other}
        assert rebuild.await_args.kwargs["collection_name"] == "kagura_memories"
        assert rebuild.await_args.kwargs["embedding_service"] is reindex_phase.embedding_service
        # No summary upsert and no chunk rewrite for the resource row.
        assert [c.kwargs["memory_id"] for c in mock_qdrant.await_args_list] == [plain.id]
        assert [c.args[1] for c in mock_write.await_args_list] == [plain]
        # Only the plain row's label-free summary was embedded here.
        assert reindex_phase.embedding_service.embed_with_usage.await_count == 1
        assert result.details["reindexed"] == 3
        assert result.embedding_calls_used == 3
        assert result.embedding_tokens == 27

    @pytest.mark.asyncio
    @patch("services.sleep.reindex.add_memory_to_qdrant", new_callable=AsyncMock)
    @patch("services.sleep.reindex.ResourceIndexer")
    async def test_failed_rebuild_counts_as_failed_and_keeps_spent_tokens(
        self, mock_indexer_cls, mock_qdrant, reindex_phase, mock_db
    ):
        resource = _make_resource_memory()
        _mock_batch_fetch(mock_db, [resource])

        async def _rebuild(memory, *, collection_name, embedding_service, usage):
            usage["tokens"] = 9
            raise RuntimeError("upsert failed")

        mock_indexer_cls.return_value.rebuild_point = AsyncMock(side_effect=_rebuild)

        result = await reindex_phase.execute(changed_memory_ids={resource.id}, user_id="user-1")

        mock_qdrant.assert_not_called()
        assert result.details["failed"] == 1
        assert result.success is False
        assert result.embedding_calls_used == 1
        assert result.embedding_tokens == 9


class TestReindexContentChangedSplit:
    """#2015 D5: only a content change re-embeds the chunk points."""

    @staticmethod
    def _write_with_usage(tokens):
        async def _write(db, memory, *, usage=None, **kwargs):
            if usage is not None:
                usage.update(cached=0, requests=1, tokens=tokens)
            return 3

        return AsyncMock(side_effect=_write)

    @pytest.mark.asyncio
    @patch("services.sleep.reindex.add_memory_to_qdrant", new_callable=AsyncMock)
    @patch("services.sleep.reindex.content_chunks_enabled", new_callable=AsyncMock)
    @patch("services.sleep.reindex.sync_content_chunk_payload", new_callable=AsyncMock)
    async def test_metadata_only_syncs_the_payload_without_a_chunk_embed(
        self, mock_sync, mock_flag, mock_qdrant, reindex_phase, mock_db
    ):
        mock_flag.return_value = True
        mock_sync.return_value = True
        meta, edited = _make_memory(importance=0.9), _make_memory()
        _mock_batch_fetch(mock_db, [meta, edited])
        reindex_phase.embedding_service.embed_with_usage = AsyncMock(return_value=([0.1], 5))
        write = self._write_with_usage(40)

        with patch("services.sleep.reindex.write_content_chunks", write):
            result = await reindex_phase.execute(
                changed_memory_ids={meta.id, edited.id},
                user_id="user-1",
                workspace_id=str(uuid4()),
                context_id=str(uuid4()),
                content_changed_memory_ids={edited.id},
            )

        # The summary is still re-embedded for both (D5 leaves that alone).
        assert mock_qdrant.await_count == 2
        # The metadata-only memory: payload synced from the new summary
        # payload, chunks not rewritten.
        mock_sync.assert_awaited_once()
        assert mock_sync.await_args.args[1] is meta
        assert mock_sync.await_args.kwargs["base_payload"]["importance"] == 0.9
        assert mock_sync.await_args.kwargs["chunks_enabled"] is True
        assert [c.args[1] for c in write.await_args_list] == [edited]
        # Two summary embeds (5 each) and one chunk batch (40).
        assert result.embedding_calls_used == 3
        assert result.embedding_tokens == 50
        assert result.details["content_chunk_rewrites"] == 1

    @pytest.mark.asyncio
    @patch("services.sleep.reindex.add_memory_to_qdrant", new_callable=AsyncMock)
    @patch("services.sleep.reindex.content_chunks_enabled", new_callable=AsyncMock)
    @patch("services.sleep.reindex.sync_content_chunk_payload", new_callable=AsyncMock)
    async def test_unknown_classification_rewrites_every_memorys_chunks(
        self, mock_sync, mock_flag, mock_qdrant, reindex_phase, mock_db
    ):
        mock_flag.return_value = True
        mem1, mem2 = _make_memory(), _make_memory()
        _mock_batch_fetch(mock_db, [mem1, mem2])
        reindex_phase.embedding_service.embed_with_usage = AsyncMock(return_value=([0.1], 0))
        write = self._write_with_usage(7)

        with patch("services.sleep.reindex.write_content_chunks", write):
            result = await reindex_phase.execute(
                changed_memory_ids={mem1.id, mem2.id},
                user_id="user-1",
                workspace_id=str(uuid4()),
                context_id=str(uuid4()),
            )

        mock_sync.assert_not_awaited()
        assert write.await_count == 2
        assert result.embedding_tokens == 14

    @pytest.mark.asyncio
    @patch("services.sleep.reindex.add_memory_to_qdrant", new_callable=AsyncMock)
    @patch("services.sleep.reindex.content_chunks_enabled", new_callable=AsyncMock)
    @patch("services.sleep.reindex.sync_content_chunk_payload", new_callable=AsyncMock)
    async def test_a_failed_payload_sync_falls_back_to_a_rewrite(
        self, mock_sync, mock_flag, mock_qdrant, reindex_phase, mock_db
    ):
        """The sync deleted the chunks it could not update: rebuild them."""
        mock_flag.return_value = True
        mock_sync.return_value = False
        mem = _make_memory()
        _mock_batch_fetch(mock_db, [mem])
        reindex_phase.embedding_service.embed_with_usage = AsyncMock(return_value=([0.1], 1))
        write = self._write_with_usage(9)

        with patch("services.sleep.reindex.write_content_chunks", write):
            result = await reindex_phase.execute(
                changed_memory_ids={mem.id},
                user_id="user-1",
                workspace_id=str(uuid4()),
                context_id=str(uuid4()),
                content_changed_memory_ids=set(),
            )

        write.assert_awaited_once()
        assert result.details["reindexed"] == 1
        assert result.embedding_tokens == 10

    @pytest.mark.asyncio
    @patch("services.sleep.reindex.add_memory_to_qdrant", new_callable=AsyncMock)
    @patch("services.sleep.reindex.content_chunks_enabled", new_callable=AsyncMock)
    async def test_chunk_tokens_count_when_the_chunk_write_raises(
        self, mock_flag, mock_qdrant, reindex_phase, mock_db
    ):
        mock_flag.return_value = True
        mem = _make_memory()
        _mock_batch_fetch(mock_db, [mem])
        reindex_phase.embedding_service.embed_with_usage = AsyncMock(return_value=([0.1], 2))

        async def _write(db, memory, *, usage=None, **kwargs):
            usage.update(cached=0, requests=1, tokens=6)
            raise RuntimeError("delete failed")

        with patch("services.sleep.reindex.write_content_chunks", AsyncMock(side_effect=_write)):
            result = await reindex_phase.execute(
                changed_memory_ids={mem.id},
                user_id="user-1",
                workspace_id=str(uuid4()),
                context_id=str(uuid4()),
            )

        assert result.details["failed"] == 1
        assert result.embedding_tokens == 8
        assert result.embedding_calls_used == 2
