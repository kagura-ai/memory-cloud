"""#2984: every writer of a point weights it with the context's keyword averages
and stores the lengths it indexed on the row.

- ``write_memory_point`` (the embedding pipeline, Sleep reindex, Sleep undo):
  the averages of ``services.keyword_stats``, the lengths on the ORM row, or in
  the caller's dict (the pipeline writes them in its fenced success UPDATE).
- A resource row through ``write_memory_point``: ``rebuild_point`` reports the
  fulltext length, stored on the row.
- The embedding-model migration (``reembed_context``): one short write per page.
- The resource indexer (``_apply_upsert``): the created / updated row.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from services import keyword_stats
from services.memory_service import build_memory_point, write_memory_point
from services.resource_indexer import resource_point_id
from utils.sparse_vector import MemoryFieldStats
from utils.tokenizer import tokenize_for_search

_SCOPE = {"user_id": "u", "workspace_id": "w", "context_id": "c"}
_STATS = MemoryFieldStats(summary=9.0, context_summary=30.0, content=200.0, summary_reading=12.0)


def _memory(**overrides) -> MagicMock:
    memory = MagicMock()
    memory.id = uuid4()
    memory.context_id = uuid4()
    memory.user_id = "u"
    memory.summary = "認証エラーの解決手順 認証"
    memory.context_summary = "なぜこの手順が必要か"
    memory.content = "トークンの期限切れで認証エラーが出る。認証を更新する。"
    memory.type = "note"
    memory.importance = 0.6
    memory.scope = "persistent"
    memory.tags = []
    memory.client = "pytest"
    memory.context = None
    memory.created_at = datetime(2026, 1, 1)
    memory.updated_at = datetime(2026, 1, 2)
    memory.location_lat = None
    memory.location_lon = None
    memory.observed_at = None
    memory.event_start = None
    memory.resource_id = None
    for column in keyword_stats.LENGTH_COLUMNS:
        setattr(memory, column, None)
    for key, value in overrides.items():
        setattr(memory, key, value)
    return memory


async def _write(memory, **kwargs):
    svc = MagicMock()
    svc.embed = AsyncMock(return_value=[0.1])
    stats = AsyncMock(return_value=_STATS)
    with (
        patch.object(keyword_stats, "memory_field_stats", stats),
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()) as upsert,
    ):
        written = await write_memory_point(
            MagicMock(), memory, collection_name="coll", embedding_service=svc, **_SCOPE, **kwargs
        )
    return written, upsert, stats


@pytest.mark.asyncio
async def test_summary_point_is_weighted_with_the_contexts_averages():
    memory = _memory()
    written, upsert, stats = await _write(memory)

    assert stats.await_args.args[1] == memory.context_id
    expected_payload, indices, values = build_memory_point(memory, _STATS)
    kwargs = upsert.await_args.kwargs
    assert dict(zip(kwargs["sparse_indices"], kwargs["sparse_values"], strict=True)) == dict(
        zip(indices, values, strict=True)
    )
    # Not the vector of the statistics-free fallback.
    _, _, fallback = build_memory_point(memory)
    assert sorted(values) != sorted(fallback)
    assert written is not None
    assert written[1] == expected_payload


@pytest.mark.asyncio
async def test_summary_point_lengths_go_on_the_orm_row():
    """Sleep reindex and undo pass no dict: their transaction flushes the row."""
    memory = _memory()
    written, _, _ = await _write(memory)
    payload = written[1]

    assert memory.keyword_summary_len == len(payload["summary_tokens"].split()) > 0
    assert memory.keyword_context_summary_len == len(payload["context_summary_tokens"].split())
    assert memory.keyword_content_len == len(payload["content_tokens"].split()) > 0
    assert memory.keyword_reading_len == len(payload["summary_reading"].split())
    assert memory.keyword_fulltext_len is None


@pytest.mark.asyncio
async def test_with_a_dict_the_lengths_go_there_and_the_row_is_untouched():
    memory = _memory()
    lengths: dict[str, int | None] = {}
    written, _, _ = await _write(memory, keyword_lengths=lengths)

    assert lengths == keyword_stats.memory_keyword_lengths(written[1])
    assert memory.keyword_summary_len is None


@pytest.mark.asyncio
async def test_no_point_written_leaves_the_lengths_alone():
    memory = _memory()
    lengths: dict[str, int | None] = {}
    with patch("services.vector_erasure.embedding_author_current", AsyncMock(return_value=False)):
        written, upsert, _ = await _write(memory, keyword_lengths=lengths)
    assert written is None
    upsert.assert_not_awaited()
    assert lengths == {}


@pytest.mark.asyncio
async def test_a_resource_rows_fulltext_length_from_the_rebuild_is_stored():
    point_id = resource_point_id("reports", "doc-1", 2)
    memory = _memory(
        summary="[resource] doc-1 v2",
        resource_id="reports",
        resource_doc_id="doc-1",
        resource_version=2,
        summary_embedding_id=point_id,
        point_id=point_id,
    )

    async def _rebuild(*_a, keyword_lengths, **_k):
        keyword_lengths.update(keyword_stats.resource_keyword_lengths("a b c d"))

    indexer = MagicMock()
    indexer.rebuild_point = AsyncMock(side_effect=_rebuild)
    await _write(memory, resource_indexer=indexer)
    assert memory.keyword_fulltext_len == 4
    assert memory.keyword_summary_len is None

    memory.keyword_fulltext_len = None
    lengths: dict[str, int | None] = {}
    await _write(memory, resource_indexer=indexer, keyword_lengths=lengths)
    assert lengths["keyword_fulltext_len"] == 4
    assert memory.keyword_fulltext_len is None


@pytest.mark.asyncio
async def test_the_pipeline_writes_the_lengths_in_its_fenced_success_update():
    """The success UPDATE (fenced on the run's claim) carries the lengths; the
    row is not flushed earlier, so its lock is not held through the chunk writes."""
    from services import memory_service
    from tests.services.test_embedding_retry_claim import (
        _chunk_flag,
        _claimed,
        _memory_row,
        _rowcount,
        _worker_db,
    )

    memory_id = uuid4()
    captured: list = []
    db = _worker_db([_claimed(memory_id), _memory_row(memory_id), _chunk_flag(False), _rowcount(1)])
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
    payload = {
        "summary_tokens": "a b",
        "context_summary_tokens": "",
        "content_tokens": "c d e",
        "summary_reading": "r",
    }
    with (
        patch("db.base.get_db", return_value=_aiter()),
        patch("services.embedding_service.EmbeddingService", MagicMock()),
        patch(
            "services.context_routing.resolve_context_routing",
            AsyncMock(return_value=("kagura_memories", embed_svc)),
        ),
        patch.object(memory_service, "add_memory_to_qdrant", AsyncMock()),
        patch.object(memory_service, "build_memory_point", return_value=(payload, [1], [1.0])),
        patch.object(memory_service, "_create_knn_seed_edges", AsyncMock()),
        patch.object(memory_service, "_create_tag_cooccurrence_seed_edges", AsyncMock()),
    ):
        await memory_service.process_pending_embedding(memory_id)

    success = str(
        captured[3].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    set_clause, where = success.split(" WHERE ", 1)
    assert "embedding_status='success'" in set_clause
    assert "keyword_summary_len=2" in set_clause
    assert "keyword_context_summary_len=0" in set_clause
    assert "keyword_content_len=3" in set_clause
    assert "keyword_reading_len=1" in set_clause
    assert "keyword_fulltext_len=NULL" in set_clause
    assert "embedding_status = 'processing'" in where


@pytest.mark.asyncio
async def test_the_model_migration_stores_each_pages_lengths():
    from services import embedding_migration_service as svc

    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    first, second = uuid4(), uuid4()
    read = {
        first: SimpleNamespace(updated_at=datetime(2026, 3, 4, 5, 6, 7), embedded_text_digest="ab"),
        second: SimpleNamespace(updated_at=None, embedded_text_digest=None),
    }
    await svc._store_keyword_lengths(
        db,
        {
            first: keyword_stats.memory_keyword_lengths({"summary_tokens": "a b"}),
            second: keyword_stats.resource_keyword_lengths("x y z"),
        },
        read,
    )
    statements = [
        str(c.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for c in db.execute.await_args_list
    ]
    assert len(statements) == 2
    assert f"WHERE memories.id = '{first}'" in statements[0]
    # Fenced on what the page read: a newer edit or pipeline write wins.
    assert "memories.updated_at IS NOT DISTINCT FROM '2026-03-04 05:06:07'" in statements[0]
    assert "memories.embedded_text_digest IS NOT DISTINCT FROM 'ab'" in statements[0]
    assert "memories.updated_at IS NOT DISTINCT FROM NULL" in statements[1]
    assert "keyword_summary_len=2" in statements[0]
    assert "keyword_fulltext_len=NULL" in statements[0]
    assert f"WHERE memories.id = '{second}'" in statements[1]
    assert "keyword_fulltext_len=3" in statements[1]
    assert "keyword_summary_len=NULL" in statements[1]
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_model_migration_weights_with_the_averages_and_collects_lengths():
    from services import embedding_migration_service as svc
    from tests.services.test_embedding_migration_service import _db, _plan, _result

    note = _memory()
    note.workspace_id = uuid4()
    plan = _plan()
    note.context_id = plan.context_id
    db = _db([_result(scalars=[note]), _result(scalars=[])])
    service = MagicMock()
    service.embed_batch = AsyncMock(return_value=[[0.1]])
    with (
        patch.object(svc, "wait_for_point_writers", new=AsyncMock(return_value=True)),
        patch.object(svc, "ensure_kagura_memories_collection", AsyncMock()),
        patch.object(svc, "add_memory_to_qdrant", AsyncMock()) as upsert,
        patch.object(svc, "rebuild_content_chunks", AsyncMock(return_value=0)),
        patch.object(svc, "_store_keyword_lengths", AsyncMock()) as stored,
        patch.object(keyword_stats, "memory_field_stats", AsyncMock(return_value=_STATS)),
        patch("services.sync_managed_guard.assert_context_mutable", AsyncMock()),
    ):
        await svc.reembed_context(db, plan, embedding_service=service)

    _, indices, values = build_memory_point(note, _STATS)
    kwargs = upsert.await_args.kwargs
    assert dict(zip(kwargs["sparse_indices"], kwargs["sparse_values"], strict=True)) == dict(
        zip(indices, values, strict=True)
    )
    ((lengths, read),) = [c.args[1:] for c in stored.await_args_list]
    assert lengths == {note.id: keyword_stats.memory_keyword_lengths(kwargs["payload"])}
    assert read == {note.id: note}


class TestResourceIndexerRow:
    """``_apply_upsert`` keeps the fulltext length on the row it creates or updates."""

    @staticmethod
    def _indexer(existing):
        from services.resource_indexer import ResourceIndexer
        from tests.services.test_resource_indexer import TestResourceIndexerNamedVectorUpsert

        db = TestResourceIndexerNamedVectorUpsert._db_with_existing(existing)
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(db)
        indexer.embedding_service = MagicMock()
        indexer.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)
        return indexer

    async def _upsert(self, indexer, avgdl):
        from tests.services.test_resource_indexer import _make_event, _make_schema

        event = _make_event()
        event.event_metadata = None
        with patch(
            "services.resource_indexer.resource_fulltext_avgdl", AsyncMock(return_value=avgdl)
        ) as read:
            await indexer._apply_upsert(
                event,
                _make_schema(),
                indexer.db.upsert_context,
                "kagura_memories",
                indexer.embedding_service,
            )
        assert read.await_args.args[1] == indexer.db.upsert_context.id
        return indexer.qdrant_client.upsert.await_args.kwargs["points"][0]

    @pytest.mark.asyncio
    async def test_a_created_row_keeps_the_length_and_the_point_uses_the_average(self):
        from utils.sparse_vector import build_resource_sparse_vector

        indexer = self._indexer(None)
        point = await self._upsert(indexer, 40.0)

        memory = indexer.db.add.call_args.args[0]
        content = point.payload["content"]
        assert memory.keyword_fulltext_len == len(tokenize_for_search(content).split()) > 0
        assert memory.keyword_summary_len is None
        bm25 = point.vector["bm25"]
        indices, values = build_resource_sparse_vector(content, 40.0)
        assert dict(zip(bm25.indices, bm25.values, strict=True)) == dict(
            zip(indices, values, strict=True)
        )

    @pytest.mark.asyncio
    async def test_an_updated_row_gets_the_length_and_loses_memory_lengths(self):
        from tests.services.test_resource_indexer import _indexed_row

        existing = _indexed_row()
        existing.tags = []
        existing.type = "fact"
        for column in keyword_stats.LENGTH_COLUMNS:
            setattr(existing, column, 5)
        indexer = self._indexer(existing)
        point = await self._upsert(indexer, None)

        content = point.payload["content"]
        assert existing.keyword_fulltext_len == len(tokenize_for_search(content).split())
        assert existing.keyword_summary_len is None
        assert existing.keyword_content_len is None


def test_no_metadata_only_path_changes_a_context_summary():
    """The keyword vector covers ``context_summary``, and a metadata-only edit
    patches the payload without rebuilding it. So every path that changes a
    row's ``context_summary`` re-embeds instead (the full point, its bm25
    vector and token fields with it): ``update_memory`` (pinned in
    tests/services/test_update_memory.py), the session rollback (a text field),
    and PATCH cannot change it at all. The erasure restore reverts details only.
    """
    from models.schemas import PatchMemoryRequest
    from services.session_rollback import _TEXT_FIELDS

    assert "context_summary" in _TEXT_FIELDS
    assert "context_summary" not in PatchMemoryRequest.model_fields
