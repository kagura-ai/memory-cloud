"""Unit tests for ResourceIndexer Qdrant upsert contract.

Issue #324: kagura_memories collection uses named vectors
({"dense": ..., "bm25": ...}); indexer must upsert with dict-keyed
vector, not anonymous.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from db.qdrant import KAGURA_MEMORIES_BM25_VECTOR_NAME, KAGURA_MEMORIES_VECTOR_NAME
from models.auth import Context, Workspace
from models.memory import Memory
from services.context_routing import resolve_context_routing
from services.resource_indexer import ResourceIndexer
from utils.datetime import utcnow


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
    async def test_apply_upsert_existing_row_lookup_excludes_tombstones(self, indexer, mock_db):
        """#1549 review: the idempotency lookup must not match a soft-deleted
        row, or a forgotten doc is patched under its tombstone (invisible, yet
        charged) instead of being re-created."""
        await indexer._apply_upsert(
            _make_event(),
            _make_schema(),
            _make_context(),
            "kagura_memories",
            indexer.embedding_service,
        )

        lookup_sql = str(mock_db.execute.call_args_list[0].args[0])
        assert "resource_doc_id" in lookup_sql
        assert "deleted_at IS NULL" in lookup_sql

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
        stripped so they can't pollute the persisted Computed columns.
        #1331 adds 'location' — connector-ingested coordinates must never
        drive the location_lat/location_lon generated columns."""
        event = _make_event()
        event.event_metadata = {
            "memory_details": {
                "channel_id": "C01",
                "external_blob": {"backend": "r2", "ref": "x"},
                "trigger": {"from": "2099", "until": "2100"},
                "location": {"lat": 35.68, "lon": 139.76},
            },
        }
        await indexer._apply_upsert(
            event, _make_schema(), _make_context(), "kagura_memories", indexer.embedding_service
        )

        memory = mock_db.add.call_args.args[0]
        assert memory.details["channel_id"] == "C01"
        assert "external_blob" not in memory.details
        assert "trigger" not in memory.details
        assert "location" not in memory.details

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
        indexer._existing_resource_doc_ids = AsyncMock(return_value=set())
        indexer.db.commit = AsyncMock()

        with (
            patch("services.resource_indexer.resolve_context_routing", mock_resolve),
            patch("services.resource_indexer.QuotaService") as quota_cls,
        ):
            quota_cls.return_value.check_memories_per_day = AsyncMock(return_value=(True, None))
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


def _upsert(doc_id: str, version: int = 1) -> MagicMock:
    event = _make_event()
    event.doc_id = doc_id
    event.version = version
    return event


class TestProcessIncrementalMemoriesPerDay:
    """#1549: connector/resource ingest is charged against the daily
    memory-creation quota ONCE per batch, up front, for the doc_ids that do
    not exist yet (a re-index of a known doc creates no row and must not burn
    the workspace's shared daily budget).

    All-or-nothing: when the batch does not fit, nothing is applied and the
    offset does not move; the job re-queues the row for the UTC reset.
    """

    def _indexer(self, events, existing: set[str] | None = None):
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(AsyncMock())
        self.state = MagicMock(last_offset=0, last_run_at=None, metrics=None)
        self.context = _make_context()
        indexer._get_or_create_state = AsyncMock(return_value=self.state)
        indexer._fetch_events = AsyncMock(return_value=events)
        indexer._get_latest_schema = AsyncMock(return_value=_make_schema())
        indexer._get_context = AsyncMock(return_value=self.context)
        indexer._existing_resource_doc_ids = AsyncMock(return_value=existing or set())
        indexer._apply_upsert = AsyncMock()
        indexer._apply_delete = AsyncMock()
        indexer.db.commit = AsyncMock()
        return indexer

    def _run(self, indexer, allowed: bool):
        quota = AsyncMock(return_value=(allowed, None if allowed else "over quota"))
        routing = AsyncMock(return_value=("kagura_memories", MagicMock()))
        return quota, routing

    @pytest.mark.asyncio
    async def test_batch_charged_once_with_new_doc_count(self):
        delete = _make_event()
        delete.op = "delete"
        indexer = self._indexer([_upsert("doc_1"), _upsert("doc_2"), delete])
        quota, routing = self._run(indexer, allowed=True)

        with (
            patch("services.resource_indexer.resolve_context_routing", routing),
            patch("services.resource_indexer.QuotaService") as quota_cls,
        ):
            quota_cls.return_value.check_memories_per_day = quota
            metrics = await indexer.process_incremental("res_test", uuid4())

        # Deletes create nothing — only the two new upserts are reserved, in one call.
        quota.assert_awaited_once_with(self.context.workspace_id, count=2)
        indexer._existing_resource_doc_ids.assert_awaited_once_with(
            "res_test", self.context, {"doc_1", "doc_2"}
        )
        assert indexer._apply_upsert.await_count == 2
        assert indexer._apply_delete.await_count == 1
        assert metrics.skipped is False

    @pytest.mark.asyncio
    async def test_all_existing_batch_charges_nothing(self):
        """A connector re-sync of N known docs is N updates, not N creations."""
        indexer = self._indexer(
            [_upsert("doc_1", 2), _upsert("doc_2", 2)], existing={"doc_1", "doc_2"}
        )
        quota, routing = self._run(indexer, allowed=False)  # would refuse if ever asked

        with (
            patch("services.resource_indexer.resolve_context_routing", routing),
            patch("services.resource_indexer.QuotaService") as quota_cls,
        ):
            quota_cls.return_value.check_memories_per_day = quota
            metrics = await indexer.process_incremental("res_test", uuid4())

        quota.assert_not_awaited()
        assert indexer._apply_upsert.await_count == 2
        assert metrics.skipped is False

    @pytest.mark.asyncio
    async def test_mixed_batch_charges_only_new_distinct_doc_ids(self):
        """doc_1 exists; doc_3 arrives twice (v1, v2) in the same batch — the
        second is an update of the row the first creates → charge 2, not 4."""
        indexer = self._indexer(
            [_upsert("doc_1", 2), _upsert("doc_2"), _upsert("doc_3", 1), _upsert("doc_3", 2)],
            existing={"doc_1"},
        )
        quota, routing = self._run(indexer, allowed=True)

        with (
            patch("services.resource_indexer.resolve_context_routing", routing),
            patch("services.resource_indexer.QuotaService") as quota_cls,
        ):
            quota_cls.return_value.check_memories_per_day = quota
            await indexer.process_incremental("res_test", uuid4())

        quota.assert_awaited_once_with(self.context.workspace_id, count=2)
        assert indexer._apply_upsert.await_count == 4

    @pytest.mark.asyncio
    async def test_refused_batch_applies_nothing_and_keeps_offset(self):
        indexer = self._indexer([_upsert("doc_1"), _upsert("doc_2")])
        quota, routing = self._run(indexer, allowed=False)

        with (
            patch("services.resource_indexer.resolve_context_routing", routing),
            patch("services.resource_indexer.QuotaService") as quota_cls,
        ):
            quota_cls.return_value.check_memories_per_day = quota
            metrics = await indexer.process_incremental("res_test", uuid4())

        indexer._apply_upsert.assert_not_awaited()
        indexer._apply_delete.assert_not_awaited()
        assert self.state.last_offset == 0
        indexer.db.commit.assert_not_awaited()
        assert metrics.skipped is True
        assert metrics.reason == "memories_per_day_exceeded"
        assert metrics.errors == 0

    @pytest.mark.asyncio
    async def test_delete_only_batch_is_not_charged(self):
        delete = _make_event()
        delete.op = "delete"
        indexer = self._indexer([delete])
        quota, routing = self._run(indexer, allowed=True)

        with (
            patch("services.resource_indexer.resolve_context_routing", routing),
            patch("services.resource_indexer.QuotaService") as quota_cls,
        ):
            quota_cls.return_value.check_memories_per_day = quota
            await indexer.process_incremental("res_test", uuid4())

        quota.assert_not_awaited()
        indexer._existing_resource_doc_ids.assert_not_awaited()
        assert indexer._apply_delete.await_count == 1


class TestExistingResourceDocIds:
    """The ONE SELECT behind the #1549 new-vs-known split."""

    def _indexer(self, rows):
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(AsyncMock())
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        indexer.db.execute = AsyncMock(return_value=result)
        return indexer

    @pytest.mark.asyncio
    async def test_returns_known_doc_ids_from_a_single_select(self):
        indexer = self._indexer(["doc_1", "doc_3"])

        known = await indexer._existing_resource_doc_ids(
            "res_test", _make_context(), {"doc_1", "doc_2", "doc_3"}
        )

        assert known == {"doc_1", "doc_3"}
        indexer.db.execute.assert_awaited_once()
        sql = str(indexer.db.execute.await_args.args[0])
        assert "resource_doc_id IN" in sql
        assert "deleted_at IS NULL" in sql

    @pytest.mark.asyncio
    async def test_empty_input_skips_the_select(self):
        indexer = self._indexer([])

        assert await indexer._existing_resource_doc_ids("res_test", _make_context(), set()) == set()
        indexer.db.execute.assert_not_awaited()


class TestResyncOfForgottenDoc:
    """#1549 review, against the real DB: a doc the user ``forget``-ed is
    re-created on re-sync as a fresh, visible row and charged exactly once —
    the tombstone neither absorbs the update nor counts as "known"."""

    async def _seed_tombstone(self, db_session):
        owner = f"owner-{uuid4().hex[:8]}"
        ws = Workspace(
            id=uuid4(), name=f"ws-{uuid4().hex[:8]}", plan_name="pro", owner_user_id=owner
        )
        db_session.add(ws)
        await db_session.flush()
        ctx = Context(
            id=uuid4(),
            workspace_id=ws.id,
            name=f"ctx-{uuid4().hex[:8]}",
            created_by=owner,
            is_private=False,
        )
        db_session.add(ctx)
        await db_session.flush()
        tombstone = Memory(
            id=uuid4(),
            user_id=owner,
            workspace_id=ws.id,
            context_id=ctx.id,
            summary="[res_test] doc_1 v1",
            content="{}",
            type="resource_data",
            client="resource_indexer",
            details={"resource_id": "res_test", "doc_id": "doc_1", "version": 1},
            deleted_at=utcnow(),
        )
        db_session.add(tombstone)
        await db_session.flush()
        return ctx, tombstone

    @pytest.mark.asyncio
    async def test_resync_creates_a_visible_row_and_is_charged_once(self, db_session):
        ctx, tombstone = await self._seed_tombstone(db_session)
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(db_session)
        indexer.embedding_service = MagicMock()
        indexer.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)
        event = _upsert("doc_1", 1)  # same version as the forgotten row
        event.event_metadata = None  # legacy shape, no worker lineage

        # Charge side: the tombstone is not "known", so the batch pays for doc_1.
        assert await indexer._existing_resource_doc_ids("res_test", ctx, {"doc_1"}) == set()

        await indexer._apply_upsert(
            event, _make_schema(), ctx, "kagura_memories", indexer.embedding_service
        )

        rows = (
            (
                await db_session.execute(
                    select(Memory).where(
                        Memory.context_id == ctx.id, Memory.resource_doc_id == "doc_1"
                    )
                )
            )
            .scalars()
            .all()
        )
        live = [m for m in rows if m.deleted_at is None]
        assert len(live) == 1
        assert live[0].id != tombstone.id
        assert live[0].resource_version == 1
        # The tombstone is left to the #1521 sweep — not restored, not reused.
        await db_session.refresh(tombstone)
        assert tombstone.deleted_at is not None
        # Now the doc is known: a second re-sync would be free.
        assert await indexer._existing_resource_doc_ids("res_test", ctx, {"doc_1"}) == {"doc_1"}
