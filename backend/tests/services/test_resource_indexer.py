"""Unit tests for ResourceIndexer Qdrant upsert contract.

Issue #324: kagura_memories collection uses named vectors
({"dense": ..., "bm25": ...}); indexer must upsert with dict-keyed
vector, not anonymous.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from db.qdrant import KAGURA_MEMORIES_BM25_VECTOR_NAME, KAGURA_MEMORIES_VECTOR_NAME
from services.context_routing import resolve_context_routing
from services.resource_indexer import ResourceIndexer


def _make_event() -> MagicMock:
    event = MagicMock()
    event.id = 1
    event.resource_id = "res_test"
    event.doc_id = "doc_1"
    event.version = 1
    event.payload = {"title": "hello", "price": 100}
    event.created_at = datetime(2026, 4, 15, tzinfo=UTC)
    event.op = "upsert"
    event.importance = None
    return event


def _make_schema() -> MagicMock:
    schema = MagicMock()
    schema.field_definitions = [
        {
            "name": "title",
            "classification": "public",
            "index_hint": "fulltext",
            "description": "Title",
        },
        {
            "name": "price",
            "classification": "public",
            "index_hint": "sort",
            "description": "Price",
        },
    ]
    return schema


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.id = uuid4()
    ctx.workspace_id = uuid4()
    ctx.created_by = uuid4()
    return ctx


class TestResourceIndexerNamedVectorUpsert:
    """Verify the Qdrant write path uses named vectors (Issue #324)."""

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        # _apply_upsert issues two queries per call: (1) existing-memory lookup,
        # (2) old-version cleanup scan. The first expects scalar_one_or_none,
        # the second iterates result.scalars().all(). Without distinct return
        # values, the second call sees a MagicMock from result 1 and the
        # `if old_memories:` branch becomes non-deterministic.
        existing = MagicMock()
        existing.scalar_one_or_none.return_value = None
        old_versions = MagicMock()
        old_versions.scalars.return_value.all.return_value = []

        call_count = 0

        def _execute_side_effect(*_args, **_kwargs):
            # Alternate per call: odd → existing-lookup, even → old-version scan.
            nonlocal call_count
            call_count += 1
            return existing if call_count % 2 == 1 else old_versions

        db.execute.side_effect = _execute_side_effect
        db.add = MagicMock()
        return db

    @pytest.fixture
    def indexer(self, mock_db):
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            idx = ResourceIndexer(mock_db)
        # Replace embedding_service with a predictable stub.
        idx.embedding_service = MagicMock()
        idx.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)
        return idx

    @pytest.mark.asyncio
    async def test_apply_upsert_sends_named_vector(self, indexer):
        """PointStruct.vector must be {"dense": <embedding>} (named), not a bare list."""
        event = _make_event()
        schema = _make_schema()
        context = _make_context()

        await indexer._apply_upsert(
            event, schema, context, "kagura_memories", indexer.embedding_service
        )

        # Qdrant upsert was called exactly once with a named-vector point.
        assert indexer.qdrant_client.upsert.await_count == 1
        call = indexer.qdrant_client.upsert.await_args
        points = call.kwargs["points"]
        assert len(points) == 1

        point = points[0]
        assert isinstance(point.vector, dict), (
            f"PointStruct.vector must be a dict for named-vector collections, got {type(point.vector)}"
        )
        assert KAGURA_MEMORIES_VECTOR_NAME in point.vector
        assert point.vector[KAGURA_MEMORIES_VECTOR_NAME] == [0.1] * 512

    @pytest.mark.asyncio
    async def test_apply_upsert_attaches_bm25_sparse_vector(self, indexer):
        """Issue #335: PointStruct.vector must include `bm25` SparseVector
        derived from the same fulltext_content as the dense embedding, so
        resource points participate in hybrid search."""
        from qdrant_client.models import SparseVector

        event = _make_event()
        schema = _make_schema()
        context = _make_context()

        await indexer._apply_upsert(
            event, schema, context, "kagura_memories", indexer.embedding_service
        )

        point = indexer.qdrant_client.upsert.await_args.kwargs["points"][0]
        assert KAGURA_MEMORIES_BM25_VECTOR_NAME in point.vector, (
            "PointStruct.vector must carry both 'dense' and 'bm25' (#335)"
        )
        bm25 = point.vector[KAGURA_MEMORIES_BM25_VECTOR_NAME]
        assert isinstance(bm25, SparseVector)
        assert len(bm25.indices) == len(bm25.values) > 0
        assert all(v > 0 for v in bm25.values)

    @pytest.mark.asyncio
    async def test_apply_upsert_uses_passed_embedding_service_not_self(self, indexer):
        """_apply_upsert must embed with the passed-in EmbeddingService, not
        with self.embedding_service. This is the #338 Layer C contract: the
        per-context service resolved by resolve_context_routing flows all
        the way into the Qdrant point vector."""
        event = _make_event()
        schema = _make_schema()
        context = _make_context()

        per_context_service = MagicMock()
        sentinel_vector = [0.777] * 512
        per_context_service.embed = AsyncMock(return_value=sentinel_vector)

        await indexer._apply_upsert(event, schema, context, "kagura_memories", per_context_service)

        per_context_service.embed.assert_awaited_once()
        indexer.embedding_service.embed.assert_not_awaited()
        point = indexer.qdrant_client.upsert.await_args.kwargs["points"][0]
        assert point.vector[KAGURA_MEMORIES_VECTOR_NAME] == sentinel_vector

    @pytest.mark.asyncio
    async def test_apply_upsert_point_id_is_deterministic_uuid(self, indexer):
        """uuid5 of resource_id:doc_id:v{version} must produce a stable point_id
        (idempotency for re-queue after Issue #324 backfill)."""
        event = _make_event()
        schema = _make_schema()
        context = _make_context()

        await indexer._apply_upsert(
            event, schema, context, "kagura_memories", indexer.embedding_service
        )
        first_id = indexer.qdrant_client.upsert.await_args.kwargs["points"][0].id

        indexer.qdrant_client.upsert.reset_mock()

        await indexer._apply_upsert(
            event, schema, context, "kagura_memories", indexer.embedding_service
        )
        second_id = indexer.qdrant_client.upsert.await_args.kwargs["points"][0].id

        assert first_id == second_id

    @pytest.mark.asyncio
    async def test_apply_upsert_passes_through_worker_lineage(self, indexer, mock_db):
        """#896: event_metadata.memory_details + source_uri project onto the
        created Memory so an ingest_event-written memory is byte-equivalent (in
        details + source_uri) to a remember()-written one. The 6 worker lineage
        keys survive and coexist with the indexer's 4 lifecycle keys."""
        event = _make_event()
        event.event_metadata = {
            "memory_details": {
                "connector_id": "c-1",
                "platform": "slack",
                "team_id": "T01",
                "channel_id": "C01",
                "thread_ts": "1700000000.0001",
                "source_message_ids": ["1700000000.0001"],
            },
            "source_uri": "slack://c-1/T01/C01/1700000000.0001",
        }
        schema = _make_schema()
        context = _make_context()

        await indexer._apply_upsert(
            event, schema, context, "kagura_memories", indexer.embedding_service
        )

        memory = mock_db.add.call_args.args[0]
        # All 6 worker lineage keys preserved (recall scope guard reads these).
        assert memory.details["channel_id"] == "C01"
        assert memory.details["thread_ts"] == "1700000000.0001"
        assert memory.details["connector_id"] == "c-1"
        assert memory.details["source_message_ids"] == ["1700000000.0001"]
        # Indexer lifecycle keys coexist (authoritative).
        assert memory.details["resource_id"] == "res_test"
        assert memory.details["doc_id"] == "doc_1"
        assert memory.details["version"] == 1
        assert "indexed_at" in memory.details
        # source_uri set so source_uri_prefix (find_by_channel) works.
        assert memory.source_uri == "slack://c-1/T01/C01/1700000000.0001"

    @pytest.mark.asyncio
    async def test_apply_upsert_without_lineage_keeps_legacy_shape(self, indexer, mock_db):
        """#896: non-worker resources (no event_metadata lineage) keep the legacy
        4-key details and NULL source_uri — backward compatible."""
        event = _make_event()
        event.event_metadata = None
        schema = _make_schema()
        context = _make_context()

        await indexer._apply_upsert(
            event, schema, context, "kagura_memories", indexer.embedding_service
        )

        memory = mock_db.add.call_args.args[0]
        assert set(memory.details.keys()) == {"resource_id", "doc_id", "version", "indexed_at"}
        assert memory.source_uri is None

    @staticmethod
    def _db_with_existing(existing_memory):
        """Build a db whose first execute() returns an existing memory (update
        path) and second returns an empty old-version scan."""
        db = AsyncMock()
        existing = MagicMock()
        existing.scalar_one_or_none.return_value = existing_memory
        old_versions = MagicMock()
        old_versions.scalars.return_value.all.return_value = []
        call_count = 0

        def _side_effect(*_a, **_k):
            nonlocal call_count
            call_count += 1
            return existing if call_count % 2 == 1 else old_versions

        db.execute.side_effect = _side_effect
        db.add = MagicMock()
        return db

    @pytest.mark.asyncio
    async def test_apply_upsert_update_path_projects_lineage(self):
        """#896: the UPDATE (re-index) branch also projects worker lineage onto
        the existing memory's details + source_uri."""
        existing_memory = MagicMock()
        db = self._db_with_existing(existing_memory)
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(db)
        indexer.embedding_service = MagicMock()
        indexer.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        event = _make_event()
        event.event_metadata = {
            "memory_details": {"channel_id": "C01", "thread_ts": "1700000000.0001"},
            "source_uri": "slack://c-1/T01/C01/1700000000.0001",
        }
        await indexer._apply_upsert(
            event, _make_schema(), _make_context(), "kagura_memories", indexer.embedding_service
        )

        assert existing_memory.details["channel_id"] == "C01"
        assert existing_memory.details["resource_id"] == "res_test"
        assert existing_memory.source_uri == "slack://c-1/T01/C01/1700000000.0001"

    @pytest.mark.asyncio
    async def test_apply_upsert_update_path_clears_stale_source_uri(self):
        """#896 (haiku review): re-indexing a doc whose event carries NO lineage
        must CLEAR a previously-set source_uri, not leave it stale (symmetry with
        the create branch)."""
        existing_memory = MagicMock()
        existing_memory.source_uri = "slack://old/stale/uri"  # from a prior index
        db = self._db_with_existing(existing_memory)
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(db)
        indexer.embedding_service = MagicMock()
        indexer.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)

        event = _make_event()
        event.event_metadata = None  # no lineage this time
        await indexer._apply_upsert(
            event, _make_schema(), _make_context(), "kagura_memories", indexer.embedding_service
        )

        assert existing_memory.source_uri is None

    @pytest.mark.asyncio
    async def test_apply_upsert_strips_computed_column_keys(self, indexer, mock_db):
        """#896 (haiku review): worker-supplied external_blob/trigger keys are
        stripped so they can't pollute the persisted Computed columns."""
        event = _make_event()
        event.event_metadata = {
            "memory_details": {
                "channel_id": "C01",
                "external_blob": {"backend": "r2", "ref": "x"},
                "trigger": {"from": "2099", "until": "2100"},
            },
        }
        await indexer._apply_upsert(
            event, _make_schema(), _make_context(), "kagura_memories", indexer.embedding_service
        )

        memory = mock_db.add.call_args.args[0]
        assert memory.details["channel_id"] == "C01"
        assert "external_blob" not in memory.details
        assert "trigger" not in memory.details

    @pytest.mark.asyncio
    async def test_apply_upsert_drops_oversized_source_uri(self, indexer, mock_db):
        """#896 (haiku review): a source_uri longer than the column width is
        dropped (None), not assigned — avoids a flush DataError poisoning the
        event offset."""
        event = _make_event()
        event.event_metadata = {"source_uri": "slack://" + "x" * 3000}
        await indexer._apply_upsert(
            event, _make_schema(), _make_context(), "kagura_memories", indexer.embedding_service
        )

        memory = mock_db.add.call_args.args[0]
        assert memory.source_uri is None

    @pytest.mark.asyncio
    async def test_apply_upsert_non_dict_memory_details_ignored(self, indexer, mock_db):
        """#896 (haiku review): a non-dict memory_details (worker schema bug) is
        dropped to the legacy 4-key shape, not crashed on."""
        event = _make_event()
        event.event_metadata = {"memory_details": 0}  # falsy non-dict
        await indexer._apply_upsert(
            event, _make_schema(), _make_context(), "kagura_memories", indexer.embedding_service
        )

        memory = mock_db.add.call_args.args[0]
        assert set(memory.details.keys()) == {"resource_id", "doc_id", "version", "indexed_at"}


class TestResolveContextRouting:
    """Issue #334 (Layer B) + #338 (Layer C) + #341 (shared helper).

    Verify the shared resolve_context_routing returns a
    (collection_name, embedding_service) tuple derived from the same
    ContextSearchConfig, so the generated embedding dim always matches the
    target collection's dim.
    """

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.fixture
    def default_service(self):
        svc = MagicMock()
        svc.model = "text-embedding-3-small"
        svc.dimensions = 512
        return svc

    @pytest.mark.asyncio
    async def test_legacy_text_embedding_3_small_returns_kagura_memories(
        self, mock_db, default_service
    ):
        cfg = MagicMock(embedding_model="text-embedding-3-small", embedding_dimensions=512)
        result = MagicMock()
        result.scalar_one_or_none.return_value = cfg
        mock_db.execute = AsyncMock(return_value=result)

        name, svc = await resolve_context_routing(mock_db, uuid4(), default_service=default_service)

        assert name == "kagura_memories"
        assert svc.model == "text-embedding-3-small"
        assert svc.dimensions == 512

    @pytest.mark.asyncio
    async def test_qwen3_8b_returns_namespaced_collection_and_matching_service(
        self, mock_db, default_service
    ):
        cfg = MagicMock(embedding_model="qwen3-embedding:8b", embedding_dimensions=4096)
        result = MagicMock()
        result.scalar_one_or_none.return_value = cfg
        mock_db.execute = AsyncMock(return_value=result)

        name, svc = await resolve_context_routing(mock_db, uuid4(), default_service=default_service)

        assert name == "kagura_memories_qwen3_embedding_8b_4096"
        assert svc.model == "qwen3-embedding:8b"
        assert svc.dimensions == 4096
        assert svc is not default_service

    @pytest.mark.asyncio
    async def test_no_search_config_falls_back_to_legacy_and_default_service(
        self, mock_db, default_service
    ):
        """When no ContextSearchConfig row exists, the resolver returns the
        legacy `kagura_memories` collection (hardcoded) paired with the
        caller-supplied default_service. The legacy collection is NOT derived
        from default_service — keeping it static guarantees all services
        read/write the same collection for legacy contexts even when an
        operator overrides settings.embedding_model."""
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=result)

        name, svc = await resolve_context_routing(mock_db, uuid4(), default_service=default_service)

        assert name == "kagura_memories"
        assert svc is default_service

    @pytest.mark.asyncio
    async def test_single_select_per_resolve_call(self, mock_db, default_service):
        """The fused resolver must issue exactly one SELECT — splitting
        collection and embedding_service back into two methods would double it."""
        cfg = MagicMock(embedding_model="qwen3-embedding:8b", embedding_dimensions=4096)
        result = MagicMock()
        result.scalar_one_or_none.return_value = cfg
        mock_db.execute = AsyncMock(return_value=result)

        await resolve_context_routing(mock_db, uuid4(), default_service=default_service)

        assert mock_db.execute.await_count == 1


class TestApplyDeleteCollectionRouting:
    """Issue #334: smoke-test that _apply_delete reaches Qdrant with the
    per-context collection_name argument for both delete paths."""

    @pytest.fixture
    def indexer(self):
        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))),
                scalar_one_or_none=MagicMock(return_value=None),
            )
        )
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            return ResourceIndexer(db)

    @pytest.mark.asyncio
    async def test_delete_all_versions_uses_passed_collection_name(self, indexer):
        event = _make_event()
        event.version = None
        await indexer._apply_delete(
            event, _make_context(), "kagura_memories_qwen3_embedding_8b_4096"
        )
        assert indexer.qdrant_client.delete.await_count == 1
        assert (
            indexer.qdrant_client.delete.await_args.kwargs["collection_name"]
            == "kagura_memories_qwen3_embedding_8b_4096"
        )

    @pytest.mark.asyncio
    async def test_delete_specific_version_uses_passed_collection_name(self, indexer):
        event = _make_event()
        event.version = 5
        await indexer._apply_delete(
            event, _make_context(), "kagura_memories_qwen3_embedding_4b_2560"
        )
        assert indexer.qdrant_client.delete.await_count == 1
        assert (
            indexer.qdrant_client.delete.await_args.kwargs["collection_name"]
            == "kagura_memories_qwen3_embedding_4b_2560"
        )


class TestProcessIncrementalResolvesRoutingOncePerBatch:
    """Issue #334 + #338: routing (collection_name + embedding_service) MUST
    be resolved once per process_incremental call (outside the per-event loop).
    If a future refactor moves the resolve into the loop, we regress into N+1
    SELECTs AND risk the two-layer bug pattern (collection/service drift)."""

    @pytest.mark.asyncio
    async def test_resolve_called_once_for_multi_event_batch(self):
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(AsyncMock())

        # Stub out everything except the helper under inspection.
        state = MagicMock(last_offset=0, last_run_at=None, metrics=None)
        indexer._get_or_create_state = AsyncMock(return_value=state)
        indexer._fetch_events = AsyncMock(
            return_value=[_make_event(), _make_event(), _make_event()]
        )
        indexer._get_latest_schema = AsyncMock(return_value=_make_schema())
        indexer._get_context = AsyncMock(return_value=_make_context())
        stub_embedding_service = MagicMock()
        mock_resolve = AsyncMock(return_value=("kagura_memories", stub_embedding_service))
        indexer._apply_upsert = AsyncMock()
        indexer._apply_delete = AsyncMock()
        indexer.db.commit = AsyncMock()

        with patch("services.resource_indexer.resolve_context_routing", mock_resolve):
            await indexer.process_incremental("res_test", uuid4())

        assert mock_resolve.await_count == 1, (
            "routing resolution must be invoked exactly once per batch, not per "
            "event — moving it inside the for-event loop is an N+1 regression."
        )
        assert indexer._apply_upsert.await_count == 3
        for call in indexer._apply_upsert.await_args_list:
            # (event, schema, context, collection_name, embedding_service)
            assert call.args[3] == "kagura_memories"
            assert call.args[4] is stub_embedding_service
