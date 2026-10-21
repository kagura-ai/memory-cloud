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
from utils.text import normalize_for_search


def _savepoint() -> AsyncMock:
    """An async context manager standing in for ``db.begin_nested()``."""
    nested = AsyncMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=None)
    return nested


def _mock_db(db: AsyncMock | None = None) -> AsyncMock:
    """A mocked session whose ``begin_nested()`` works under ``async with``.

    An ``AsyncMock().begin_nested()`` returns a coroutine, which breaks
    ``async with`` (#2218: every event, and the old-version cleanup, runs in
    a SAVEPOINT).
    """
    db = db if db is not None else AsyncMock()
    db.begin_nested = MagicMock(side_effect=lambda: _savepoint())
    return db


def _indexed_row() -> MagicMock:
    """A live row the indexer wrote and nobody edited since (``version == 1``)."""
    row = MagicMock()
    row.id = uuid4()
    row.version = 1
    row.source = "resource_ingest"
    row.details = {}
    return row


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
        return _mock_db(db)

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
    async def test_apply_upsert_point_carries_the_existing_rows_memory_id(self, indexer, mock_db):
        """#1829: on a re-index the payload memory_id is the row's id, not a
        fresh uuid4() written before the row was looked up (#1808)."""
        existing = _indexed_row()
        lookup = MagicMock()
        lookup.scalar_one_or_none.return_value = existing
        old_versions = MagicMock()
        old_versions.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [lookup, old_versions]

        await indexer._apply_upsert(
            _make_event(),
            _make_schema(),
            _make_context(),
            "kagura_memories",
            indexer.embedding_service,
        )

        point = indexer.qdrant_client.upsert.await_args.kwargs["points"][0]
        assert point.payload["memory_id"] == str(existing.id)
        # The row was resolved before the point was built: one lookup, no second.
        assert mock_db.add.call_count == 0

    @pytest.mark.asyncio
    async def test_apply_upsert_payload_carries_the_write_time_next_to_the_event_time(
        self, indexer
    ):
        """#1869: ``updated_at`` stays the event time (the updated_after /
        updated_before recall filters read it); ``indexed_at`` is when the point
        was written, which is what the orphan sweep's grace period needs."""
        before = utcnow()

        await indexer._apply_upsert(
            _make_event(),
            _make_schema(),
            _make_context(),
            "kagura_memories",
            indexer.embedding_service,
        )

        payload = indexer.qdrant_client.upsert.await_args.kwargs["points"][0].payload
        assert payload["updated_at"] == "2026-04-15T00:00:00Z"
        assert payload["indexed_at"].endswith("Z")
        indexed_at = datetime.fromisoformat(payload["indexed_at"].replace("Z", "+00:00"))
        assert before <= indexed_at.replace(tzinfo=None) <= utcnow()

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
        return _mock_db(db)

    @pytest.mark.asyncio
    async def test_apply_upsert_update_path_projects_lineage(self):
        """#896: the UPDATE (re-index) branch also projects worker lineage onto
        the existing memory's details + source_uri."""
        existing_memory = _indexed_row()
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
        existing_memory = _indexed_row()
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
        drive the location_lat/location_lon generated columns. 'tool_trigger'
        marks a tool guardrail — connector-ingested content must never become
        an instruction a client hook injects."""
        event = _make_event()
        event.event_metadata = {
            "memory_details": {
                "channel_id": "C01",
                "external_blob": {"backend": "r2", "ref": "x"},
                "trigger": {"from": "2099", "until": "2100"},
                "location": {"lat": 35.68, "lon": 139.76},
                "tool_trigger": {"tool": "Bash", "match": "rm -rf", "action": "block"},
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
        assert "tool_trigger" not in memory.details

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
            indexer = ResourceIndexer(_mock_db())

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
        indexer._plan_suspended = AsyncMock(return_value=False)
        indexer._workspace_memory_room = AsyncMock(return_value=10_000)
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
            indexer = ResourceIndexer(_mock_db())
        self.state = MagicMock(last_offset=0, last_run_at=None, metrics=None)
        self.context = _make_context()
        indexer._get_or_create_state = AsyncMock(return_value=self.state)
        indexer._fetch_events = AsyncMock(return_value=events)
        indexer._get_latest_schema = AsyncMock(return_value=_make_schema())
        indexer._get_context = AsyncMock(return_value=self.context)
        indexer._existing_resource_doc_ids = AsyncMock(return_value=existing or set())
        indexer._apply_upsert = AsyncMock()
        indexer._apply_delete = AsyncMock()
        indexer._plan_suspended = AsyncMock(return_value=False)
        indexer._workspace_memory_room = AsyncMock(return_value=10_000)
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


def _delete(doc_id: str, event_id: int, version: int | None = None) -> MagicMock:
    event = _make_event()
    event.op = "delete"
    event.doc_id = doc_id
    event.version = version
    event.id = event_id
    return event


def _numbered(*events: MagicMock) -> list[MagicMock]:
    """Give each event a distinct, increasing id (the offset the batch advances to)."""
    for event_id, event in enumerate(events, start=1):
        event.id = event_id
        # Naive, like the TIMESTAMP WITHOUT TIME ZONE column the lag is read from.
        event.created_at = datetime(2026, 4, 15)
    return list(events)


class TestProcessIncrementalSuspendedOrFull:
    """#1939: a Free-again workspace's events are kept, not dropped.

    A suspended resource (the plan no longer carries connectors / resources)
    applies nothing and leaves the offset where it was. Against the workspace
    memory limit the batch applies its longest prefix (in event order) that
    fits, so a nearly full workspace still makes progress — and the deletes in
    that prefix free room for the upserts after them. The job re-queues the
    row for whatever is left.
    """

    def _indexer(self, events, *, suspended: bool = False, room: int = 10_000, existing=None):
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(_mock_db())
        self.state = MagicMock(last_offset=0, last_run_at=None, metrics=None)
        self.context = _make_context()
        indexer._get_or_create_state = AsyncMock(return_value=self.state)
        indexer._fetch_events = AsyncMock(return_value=events)
        indexer._get_latest_schema = AsyncMock(return_value=_make_schema())
        indexer._get_context = AsyncMock(return_value=self.context)
        indexer._existing_resource_doc_ids = AsyncMock(return_value=set(existing or ()))
        indexer._apply_upsert = AsyncMock()
        indexer._apply_delete = AsyncMock()
        indexer._plan_suspended = AsyncMock(return_value=suspended)
        indexer._workspace_memory_room = AsyncMock(return_value=room)
        indexer.db.commit = AsyncMock()
        return indexer

    async def _process(self, indexer):
        routing = AsyncMock(return_value=("kagura_memories", MagicMock()))
        with (
            patch("services.resource_indexer.resolve_context_routing", routing),
            patch("services.resource_indexer.QuotaService") as quota_cls,
        ):
            self.daily_quota = AsyncMock(return_value=(True, None))
            quota_cls.return_value.check_memories_per_day = self.daily_quota
            return await indexer.process_incremental("res_test", uuid4())

    def _applied_doc_ids(self, indexer) -> list[str]:
        return [c.args[0].doc_id for c in indexer._apply_upsert.await_args_list]

    @pytest.mark.asyncio
    async def test_suspended_resource_applies_nothing_and_keeps_offset(self):
        indexer = self._indexer([_upsert("doc_1")], suspended=True)
        metrics = await self._process(indexer)

        assert metrics.skipped is True
        assert metrics.reason == "plan_suspended"
        indexer._apply_upsert.assert_not_awaited()
        assert self.state.last_offset == 0
        self.daily_quota.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_room_and_a_new_doc_first_holds_back_without_daily_charge(self):
        indexer = self._indexer(_numbered(_upsert("doc_1"), _upsert("doc_2")), room=0)
        metrics = await self._process(indexer)

        assert metrics.skipped is True
        assert metrics.reason == "memory_limit_exceeded"
        indexer._apply_upsert.assert_not_awaited()
        assert self.state.last_offset == 0
        # The memory limit is read first, so a refused batch burns no daily budget.
        self.daily_quota.assert_not_awaited()
        indexer._workspace_memory_room.assert_awaited_once_with(self.context.workspace_id)

    @pytest.mark.asyncio
    async def test_the_prefix_that_fits_is_applied_and_the_offset_advances(self):
        events = _numbered(*(_upsert(f"doc_{n}") for n in range(1, 5)))
        indexer = self._indexer(events, room=2)
        metrics = await self._process(indexer)

        assert self._applied_doc_ids(indexer) == ["doc_1", "doc_2"]
        assert self.state.last_offset == 2
        self.daily_quota.assert_awaited_once_with(self.context.workspace_id, count=2)
        # Progress was made, so not "skipped"; the reason re-queues the rest.
        assert metrics.skipped is False
        assert metrics.reason == "memory_limit_exceeded"
        indexer.db.commit.assert_awaited()

    @pytest.mark.asyncio
    async def test_deletes_before_the_first_non_fitting_upsert_apply_and_free_room(self):
        """At the limit: deleting a known doc frees one slot for the next new doc."""
        events = _numbered(
            _delete("doc_old", 0),  # known doc, all versions → frees a slot
            _upsert("doc_new_1"),  # takes that slot
            _delete("doc_other", 0, version=3),  # one version only: frees nothing
            _upsert("doc_new_2"),  # does not fit → batch stops here
            _delete("doc_after", 0),
        )
        indexer = self._indexer(events, room=0, existing={"doc_old", "doc_other"})
        metrics = await self._process(indexer)

        assert indexer._apply_delete.await_count == 2
        assert self._applied_doc_ids(indexer) == ["doc_new_1"]
        assert self.state.last_offset == 3
        assert metrics.reason == "memory_limit_exceeded"
        self.daily_quota.assert_awaited_once_with(self.context.workspace_id, count=1)

    @pytest.mark.asyncio
    async def test_updates_of_known_docs_apply_with_no_room(self):
        events = _numbered(_upsert("doc_known", 2), _upsert("doc_new"))
        indexer = self._indexer(events, room=0, existing={"doc_known"})
        metrics = await self._process(indexer)

        assert self._applied_doc_ids(indexer) == ["doc_known"]
        assert self.state.last_offset == 1
        assert metrics.skipped is False
        # The prefix creates nothing, so there is nothing to charge.
        self.daily_quota.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_batch_that_fits_is_applied_whole(self):
        delete = _delete("doc_x", 0)
        events = _numbered(_upsert("doc_1"), delete, _upsert("doc_2"), _upsert("doc_3"))
        indexer = self._indexer(events, room=3)
        metrics = await self._process(indexer)

        assert self._applied_doc_ids(indexer) == ["doc_1", "doc_2", "doc_3"]
        assert indexer._apply_delete.await_count == 1
        assert self.state.last_offset == 4
        assert metrics.skipped is False
        assert metrics.reason is None
        self.daily_quota.assert_awaited_once_with(self.context.workspace_id, count=3)

    @pytest.mark.asyncio
    async def test_a_batch_that_creates_nothing_never_reads_the_limit(self):
        indexer = self._indexer(_numbered(_delete("doc_1", 0)), room=0)
        metrics = await self._process(indexer)

        indexer._workspace_memory_room.assert_not_awaited()
        assert indexer._apply_delete.await_count == 1
        assert metrics.skipped is False

    @pytest.mark.asyncio
    async def test_plan_suspended_reads_the_workspace(self):
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(_mock_db())
        workspace = Workspace(id=uuid4(), name="w", plan_name="free", owner_user_id="u")
        indexer.db.get = AsyncMock(return_value=workspace)
        context = _make_context()
        with patch(
            "services.resource_indexer.ingest_suspended", new=AsyncMock(return_value=True)
        ) as check:
            assert await indexer._plan_suspended(context, "pk") is True
        check.assert_awaited_once_with(indexer.db, workspace, "pk")


class TestWorkspaceMemoryRoom:
    """#1939: the indexer's room is the workspace's OWN live memory count
    against its effective limit — not the member-join count the remember
    path uses, which also counts the members' memories in other workspaces."""

    @pytest.mark.asyncio
    async def test_counts_only_this_workspaces_live_memories(self, db_session):
        owner = f"owner-{uuid4().hex[:8]}"
        ws = Workspace(
            id=uuid4(), name=f"ws-{uuid4().hex[:8]}", plan_name="free", owner_user_id=owner
        )
        other = Workspace(
            id=uuid4(), name=f"ws-{uuid4().hex[:8]}", plan_name="free", owner_user_id=owner
        )
        db_session.add_all([ws, other])
        await db_session.flush()

        def _memory(workspace_id, **kwargs):
            return Memory(
                id=uuid4(),
                user_id=owner,
                workspace_id=workspace_id,
                summary="s",
                content="c",
                type="code",
                client="pytest",
                **kwargs,
            )

        db_session.add_all(
            [
                _memory(ws.id),
                _memory(ws.id),
                _memory(ws.id, deleted_at=utcnow()),  # forgotten: frees its slot
                _memory(other.id),  # the same owner's memory elsewhere
                _memory(other.id),
            ]
        )
        await db_session.flush()

        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(db_session)

        assert await indexer._workspace_memory_room(ws.id) == ws.effective_memory_limit - 2

    @pytest.mark.asyncio
    async def test_missing_workspace_has_no_room(self, db_session):
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(db_session)

        assert await indexer._workspace_memory_room(uuid4()) == 0


class TestExistingResourceDocIds:
    """The ONE SELECT behind the #1549 new-vs-known split."""

    def _indexer(self, rows):
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(_mock_db())
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


def _projected() -> dict:
    return {"fulltext_content": "Title: hello", "facets": {}, "sortable": {}, "metadata": {}}


def _point(**row_metadata):
    from services.resource_indexer import build_resource_point

    context = _make_context()
    memory_id = uuid4()
    point = build_resource_point(
        context=context,
        resource_id="res_test",
        doc_id="doc_1",
        version=1,
        content="Title: hello",
        projected=_projected(),
        embedding=[0.1] * 512,
        updated_at=datetime(2026, 4, 15, tzinfo=UTC),
        memory_id=memory_id,
        **row_metadata,
    )
    return point, context, memory_id


class TestBuildResourcePointRowMetadata:
    """#2160: the point carries the owning row's editable metadata when given."""

    def test_all_four_keys_land_in_the_payload(self):
        point, _, _ = _point(
            tags=["a", "b"],
            memory_type="fact",
            context_summary="edited",
            location={"lat": 1.5, "lon": 2.5},
        )
        assert point.payload["tags"] == ["a", "b"]
        assert point.payload["type"] == "fact"
        assert point.payload["context_summary"] == "edited"
        assert point.payload["location"] == {"lat": 1.5, "lon": 2.5}

    def test_context_summary_is_normalized_like_the_memory_point(self):
        point, _, _ = _point(context_summary="ｶﾀｶﾅ ①")
        assert point.payload["context_summary"] == "カタカナ 1"

    def test_no_metadata_keeps_the_create_shape(self):
        point, _, _ = _point()
        assert not {"tags", "type", "context_summary", "location"} & set(point.payload)

    def test_metadata_never_shadows_identity_keys(self):
        # Only the editable keys exist as arguments, and they are merged under
        # the indexer keys: the identity keys are the context's and the row's.
        point, context, memory_id = _point(tags=["x"], memory_type="fact")
        assert point.payload["context_id"] == str(context.id)
        assert point.payload["workspace_id"] == str(context.workspace_id)
        assert point.payload["memory_id"] == str(memory_id)


class TestApplyUpsertRowMetadata:
    """#2160: a same-version re-index writes the row's post-update metadata."""

    @staticmethod
    def _indexer(existing_memory):
        db = TestResourceIndexerNamedVectorUpsert._db_with_existing(existing_memory)
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(db)
        indexer.embedding_service = MagicMock()
        indexer.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)
        return indexer

    @staticmethod
    def _existing():
        existing = _indexed_row()
        existing.tags = ["a"]
        existing.type = "fact"
        existing.context_summary = "the indexer's old text"
        # A stale pre-update value of the generated columns: never the source.
        existing.location_lat = 10.0
        existing.location_lon = 20.0
        return existing

    async def _upsert(self, indexer, event):
        await indexer._apply_upsert(
            event, _make_schema(), _make_context(), "kagura_memories", indexer.embedding_service
        )
        return indexer.qdrant_client.upsert.await_args.kwargs["points"][0].payload

    @pytest.mark.asyncio
    async def test_update_branch_rewrites_an_unedited_context_summary(self):
        existing = self._existing()
        indexer = self._indexer(existing)
        event = _make_event()
        event.event_metadata = None

        payload = await self._upsert(indexer, event)

        assert payload["tags"] == ["a"]
        assert payload["type"] == "fact"
        assert payload["context_summary"] == existing.context_summary == "Title: hello"
        # The new details carry no location, so neither does the point, even
        # though the stale columns still hold one.
        assert "location" not in payload

    @pytest.mark.asyncio
    async def test_update_branch_location_comes_from_the_new_details(self):
        """The location follows the details the row is left with. A worker
        cannot supply one (#1331 strips it from the lineage), so the re-indexed
        row loses its coordinate in PG and the point must not keep it either,
        however the stale generated columns read."""
        existing = self._existing()
        indexer = self._indexer(existing)
        event = _make_event()
        event.event_metadata = {"memory_details": {"location": {"lat": 35, "lon": 139.5}}}

        payload = await self._upsert(indexer, event)

        assert "location" not in existing.details
        assert "location" not in payload

    @pytest.mark.asyncio
    async def test_update_branch_truncates_context_summary_like_the_row(self):
        existing = self._existing()
        indexer = self._indexer(existing)
        event = _make_event()
        event.event_metadata = None
        event.payload = {"title": "x" * 3000, "price": 1}

        payload = await self._upsert(indexer, event)

        assert len(payload["context_summary"]) == 2000
        assert payload["context_summary"].endswith("...")
        assert payload["context_summary"] == existing.context_summary

    @pytest.mark.asyncio
    async def test_update_branch_keeps_an_edited_context_summary(self):
        """#2218: an API edit bumped ``version``; the re-index keeps the text."""
        existing = self._existing()
        existing.version = 2
        existing.context_summary = "What the user wrote"
        indexer = self._indexer(existing)
        event = _make_event()
        event.event_metadata = None

        payload = await self._upsert(indexer, event)

        assert existing.context_summary == "What the user wrote"
        assert payload["context_summary"] == normalize_for_search("What the user wrote")

    @pytest.mark.asyncio
    async def test_update_branch_keeps_a_remembered_rows_context_summary(self):
        """#2218: a ``remember(external_id)`` row (#2164) was never the indexer's."""
        existing = self._existing()
        existing.source = "mcp_remember"
        existing.context_summary = "A remembered summary"
        indexer = self._indexer(existing)
        event = _make_event()
        event.event_metadata = None

        payload = await self._upsert(indexer, event)

        assert existing.context_summary == "A remembered summary"
        assert payload["context_summary"] == normalize_for_search("A remembered summary")

    @pytest.mark.asyncio
    async def test_update_branch_falls_back_when_the_kept_summary_is_empty(self):
        existing = self._existing()
        existing.version = 2
        existing.context_summary = None
        indexer = self._indexer(existing)
        event = _make_event()
        event.event_metadata = None

        payload = await self._upsert(indexer, event)

        assert existing.context_summary == "Title: hello"
        assert payload["context_summary"] == "Title: hello"

    @pytest.mark.asyncio
    async def test_update_branch_keeps_details_location_in_the_row_and_the_point(self):
        """#2218: only a user writes ``details.location``; a re-index keeps it."""
        existing = self._existing()
        existing.details = {
            "resource_id": "res_test",
            "location": {"lat": 35.5, "lon": 139.25},
            "other": 1,
        }
        indexer = self._indexer(existing)
        event = _make_event()
        event.event_metadata = None

        payload = await self._upsert(indexer, event)

        assert existing.details["location"] == {"lat": 35.5, "lon": 139.25}
        assert "other" not in existing.details
        assert existing.details["resource_id"] == "res_test"
        assert payload["location"] == {"lat": 35.5, "lon": 139.25}

    @pytest.mark.asyncio
    async def test_update_branch_keeps_the_rows_location_over_a_worker_supplied_one(self):
        existing = self._existing()
        existing.details = {"location": {"lat": 1.0, "lon": 2.0}}
        indexer = self._indexer(existing)
        event = _make_event()
        event.event_metadata = {"memory_details": {"location": {"lat": 35, "lon": 139.5}}}

        payload = await self._upsert(indexer, event)

        assert existing.details["location"] == {"lat": 1.0, "lon": 2.0}
        assert payload["location"] == {"lat": 1.0, "lon": 2.0}

    @pytest.mark.asyncio
    async def test_create_branch_point_carries_the_rows_defaults(self):
        """#2218: a fresh point matches ``type`` / ``tags`` filters like a
        re-indexed or rebuilt one."""
        indexer = self._indexer(None)
        event = _make_event()
        event.event_metadata = None

        payload = await self._upsert(indexer, event)

        memory = indexer.db.add.call_args.args[0]
        assert memory.type == "resource_data"
        assert memory.tags == []
        assert memory.source == "resource_ingest"
        assert payload["type"] == memory.type
        assert payload["tags"] == memory.tags
        assert payload["context_summary"] == normalize_for_search(memory.context_summary)
        assert "location" not in payload


class TestRepointedRowDropsItsStrayPoints:
    """#2164: a row re-pointed onto its document's point loses its chunk points
    and its own-id point, after the batch commits and never on rollback."""

    COLLECTION = "kagura_memories"

    def _run(self, previous_point, *, commit_error=None):
        """process_incremental over one same-version re-index of a row that
        named ``previous_point`` (``"own"`` = the row id, ``"uuid5"`` = the
        document's point)."""
        from services.resource_indexer import resource_point_id

        event = _make_event()
        event.event_metadata = None
        # Naive, like ResourceEvent.created_at: the run computes lag against utcnow().
        event.created_at = utcnow()
        existing = _indexed_row()
        existing.tags = []
        existing.type = "fact"
        doc_point = resource_point_id(event.resource_id, event.doc_id, event.version)
        existing.summary_embedding_id = {"own": existing.id, "uuid5": doc_point}.get(
            previous_point, previous_point
        )
        db = TestResourceIndexerNamedVectorUpsert._db_with_existing(existing)
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(db)
        indexer.embedding_service = MagicMock()
        indexer.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)
        state = MagicMock(last_offset=0, last_run_at=None, metrics=None)
        indexer._get_or_create_state = AsyncMock(return_value=state)
        indexer._fetch_events = AsyncMock(return_value=[event])
        indexer._get_latest_schema = AsyncMock(return_value=_make_schema())
        indexer._get_context = AsyncMock(return_value=_make_context())
        indexer._existing_resource_doc_ids = AsyncMock(return_value={event.doc_id})
        indexer._plan_suspended = AsyncMock(return_value=False)

        order: list[str] = []
        db.commit = AsyncMock(
            side_effect=commit_error or (lambda: order.append("commit")),
        )
        db.rollback = AsyncMock()
        chunks = AsyncMock(side_effect=lambda *a, **k: order.append("chunks"))
        points = AsyncMock(side_effect=lambda *a, **k: order.append("points"))
        return indexer, existing, chunks, points, order

    async def _process(self, indexer, chunks, points):
        routing = AsyncMock(return_value=(self.COLLECTION, indexer.embedding_service))
        with (
            patch("services.resource_indexer.resolve_context_routing", routing),
            patch("services.resource_lookup.resolve_resource_pk", AsyncMock(return_value=1)),
            patch("services.resource_indexer.delete_chunk_points", chunks),
            patch("services.resource_indexer.delete_points_from_qdrant", points),
            patch("services.resource_indexer.logger") as log,
        ):
            metrics = await indexer.process_incremental("res_test", uuid4())
        return metrics, log

    @pytest.mark.asyncio
    async def test_own_id_previous_point_drops_chunks_and_point_after_commit(self):
        indexer, existing, chunks, points, order = self._run("own")

        metrics, log = await self._process(indexer, chunks, points)

        assert metrics.errors == 0
        chunks.assert_awaited_once_with([existing.id], self.COLLECTION)
        points.assert_awaited_once_with([str(existing.id)], self.COLLECTION)
        assert order == ["commit", "chunks", "points"]
        assert indexer._repointed == []
        events = [c.args[0] for c in log.info.call_args_list]
        assert "resource_repoint_points_dropped" in events

    @pytest.mark.asyncio
    async def test_already_owned_point_deletes_nothing(self):
        indexer, _, chunks, points, _ = self._run("uuid5")

        await self._process(indexer, chunks, points)

        chunks.assert_not_awaited()
        points.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_previous_point_drops_chunks_and_own_id(self):
        indexer, existing, chunks, points, _ = self._run(None)

        await self._process(indexer, chunks, points)

        chunks.assert_awaited_once_with([existing.id], self.COLLECTION)
        points.assert_awaited_once_with([str(existing.id)], self.COLLECTION)

    @pytest.mark.asyncio
    async def test_another_previous_point_is_kept(self):
        other = uuid4()
        indexer, existing, chunks, points, _ = self._run(other)

        _, log = await self._process(indexer, chunks, points)

        chunks.assert_awaited_once_with([existing.id], self.COLLECTION)
        points.assert_not_awaited()
        kept = [
            c
            for c in log.info.call_args_list
            if c.args[0] == "resource_repoint_previous_point_kept"
        ]
        assert kept and kept[0].kwargs["point_id"] == str(other)

    @pytest.mark.asyncio
    async def test_a_failed_delete_is_logged_and_the_run_completes(self):
        indexer, existing, _, points, _ = self._run("own")
        chunks = AsyncMock(side_effect=RuntimeError("qdrant down"))

        metrics, log = await self._process(indexer, chunks, points)

        assert metrics.errors == 0
        points.assert_not_awaited()
        warned = [
            c for c in log.warning.call_args_list if c.args[0] == "resource_repoint_cleanup_failed"
        ]
        assert warned[0].kwargs == {
            "memory_id": str(existing.id),
            "collection": self.COLLECTION,
            "error_class": "RuntimeError",
        }
        assert indexer._repointed == []

    @pytest.mark.asyncio
    async def test_a_rolled_back_batch_deletes_nothing(self):
        indexer, _, chunks, points, _ = self._run("own", commit_error=RuntimeError("commit failed"))

        metrics, _ = await self._process(indexer, chunks, points)

        assert metrics.errors == 1
        indexer.db.rollback.assert_awaited_once()
        chunks.assert_not_awaited()
        points.assert_not_awaited()
        assert indexer._repointed == []


class TestProcessIncrementalSavepoints:
    """#2218: every event runs in its own SAVEPOINT, so a failed event rolls
    back only itself and the session stays usable for the rest of the batch."""

    COLLECTION = "kagura_memories"

    def _indexer(self, events):
        db = AsyncMock()
        self.savepoints: list[AsyncMock] = []

        def _begin_nested():
            nested = _savepoint()
            self.savepoints.append(nested)
            return nested

        db.begin_nested = MagicMock(side_effect=_begin_nested)
        db.commit = AsyncMock()
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(db)
        self.state = MagicMock(last_offset=0, last_run_at=None, metrics=None)
        indexer._get_or_create_state = AsyncMock(return_value=self.state)
        indexer._fetch_events = AsyncMock(return_value=events)
        indexer._get_latest_schema = AsyncMock(return_value=_make_schema())
        indexer._get_context = AsyncMock(return_value=_make_context())
        indexer._existing_resource_doc_ids = AsyncMock(return_value=set())
        indexer._apply_upsert = AsyncMock()
        indexer._apply_delete = AsyncMock()
        indexer._plan_suspended = AsyncMock(return_value=False)
        indexer._workspace_memory_room = AsyncMock(return_value=10_000)
        return indexer

    async def _process(self, indexer, chunks=None, points=None):
        routing = AsyncMock(return_value=(self.COLLECTION, MagicMock()))
        with (
            patch("services.resource_indexer.resolve_context_routing", routing),
            patch("services.resource_indexer.QuotaService") as quota_cls,
            patch("services.resource_indexer.delete_chunk_points", chunks or AsyncMock()),
            patch("services.resource_indexer.delete_points_from_qdrant", points or AsyncMock()),
        ):
            quota_cls.return_value.check_memories_per_day = AsyncMock(return_value=(True, None))
            return await indexer.process_incremental("res_test", uuid4())

    @pytest.mark.asyncio
    async def test_each_event_runs_in_its_own_savepoint(self):
        indexer = self._indexer(_numbered(_upsert("doc_1"), _upsert("doc_2"), _upsert("doc_3")))

        metrics = await self._process(indexer)

        assert len(self.savepoints) == 3
        for nested in self.savepoints:
            nested.__aenter__.assert_awaited_once()
            nested.__aexit__.assert_awaited_once()
            assert nested.__aexit__.await_args.args[0] is None
        assert metrics.applied_upserts == 3
        assert self.state.last_offset == 3

    @pytest.mark.asyncio
    async def test_a_failed_event_rolls_back_only_itself(self):
        events = _numbered(_upsert("doc_1"), _upsert("doc_2"), _upsert("doc_3"))
        indexer = self._indexer(events)
        indexer._apply_upsert.side_effect = [None, RuntimeError("boom"), None]

        metrics = await self._process(indexer)

        # The second savepoint saw the exception (its rollback), the others none.
        assert self.savepoints[1].__aexit__.await_args.args[0] is RuntimeError
        assert self.savepoints[0].__aexit__.await_args.args[0] is None
        assert self.savepoints[2].__aexit__.await_args.args[0] is None
        assert metrics.applied_upserts == 2
        assert metrics.errors == 1
        assert self.state.last_offset == events[2].id
        indexer.db.commit.assert_awaited_once()
        indexer.db.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failed_events_repoints_are_dropped(self):
        events = _numbered(_upsert("doc_1"), _upsert("doc_2"))
        indexer = self._indexer(events)
        kept_id, dropped_id = uuid4(), uuid4()

        async def _apply(event, *_a):
            if event.doc_id == "doc_1":
                indexer._repointed.append((kept_id, kept_id, self.COLLECTION))
                return
            indexer._repointed.append((dropped_id, dropped_id, self.COLLECTION))
            raise RuntimeError("flush failed after the re-point")

        indexer._apply_upsert.side_effect = _apply
        chunks, points = AsyncMock(), AsyncMock()

        metrics = await self._process(indexer, chunks, points)

        assert metrics.errors == 1
        chunks.assert_awaited_once_with([kept_id], self.COLLECTION)
        points.assert_awaited_once_with([str(kept_id)], self.COLLECTION)
        assert indexer._repointed == []

    @pytest.mark.asyncio
    async def test_a_failed_release_does_not_count_the_event(self):
        events = _numbered(_upsert("doc_1"), _upsert("doc_2"))
        indexer = self._indexer(events)
        release_error = RuntimeError("RELEASE SAVEPOINT failed")
        original = indexer.db.begin_nested.side_effect

        def _begin_nested():
            nested = original()
            if len(self.savepoints) == 2:
                nested.__aexit__ = AsyncMock(side_effect=release_error)
            return nested

        indexer.db.begin_nested.side_effect = _begin_nested

        metrics = await self._process(indexer)

        assert metrics.applied_upserts == 1
        assert metrics.errors == 1
        assert self.state.last_offset == events[0].id

    @pytest.mark.asyncio
    async def test_old_version_cleanup_runs_in_its_own_savepoint(self):
        """Step 6 is non-blocking: a failed cleanup rolls back only itself."""
        old = MagicMock()
        old.resource_version = 0
        db = AsyncMock()
        existing = MagicMock()
        existing.scalar_one_or_none.return_value = None
        old_versions = MagicMock()
        old_versions.scalars.return_value.all.return_value = [old]
        db.execute.side_effect = [existing, old_versions]
        db.add = MagicMock()
        db.delete = AsyncMock(side_effect=RuntimeError("cleanup flush failed"))
        cleanup = _savepoint()
        db.begin_nested = MagicMock(return_value=cleanup)
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(db)
        indexer.embedding_service = MagicMock()
        indexer.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)
        event = _make_event()
        event.event_metadata = None

        with patch("services.resource_indexer.logger") as log:
            await indexer._apply_upsert(
                event, _make_schema(), _make_context(), self.COLLECTION, indexer.embedding_service
            )

        db.begin_nested.assert_called_once()
        assert cleanup.__aexit__.await_args.args[0] is RuntimeError
        db.add.assert_called_once()
        warned = [c.args[0] for c in log.warning.call_args_list]
        assert "old_version_cleanup_failed" in warned


async def _seed_indexed_resource(db_session, events: list[dict]):
    """A workspace, context, resource and schema with ``events`` queued.

    Committed: ``process_incremental`` commits, so the caller must hand the
    returned ids to ``_drop_indexed_resource``.
    """
    from models.resource import Resource, ResourceEvent, ResourceSchema

    owner = f"owner-{uuid4().hex[:8]}"
    slug = f"res-{uuid4().hex[:8]}"
    ws = Workspace(id=uuid4(), name=f"ws-{uuid4().hex[:8]}", plan_name="pro", owner_user_id=owner)
    ctx = Context(
        id=uuid4(),
        workspace_id=ws.id,
        name=f"ctx-{uuid4().hex[:8]}",
        created_by=owner,
        is_private=False,
        resource_id=slug,
    )
    resource = Resource(id=uuid4(), workspace_id=ws.id, resource_id=slug)
    db_session.add(ws)
    await db_session.flush()
    db_session.add_all([ctx, resource])
    await db_session.flush()
    db_session.add(
        ResourceSchema(
            resource_pk=resource.id,
            resource_id=slug,
            schema_version=1,
            field_definitions=[
                {"name": "title", "classification": "public", "index_hint": "fulltext"}
            ],
        )
    )
    for spec in events:
        db_session.add(
            ResourceEvent(
                resource_pk=resource.id,
                resource_id=slug,
                op="upsert",
                created_at=utcnow(),
                **spec,
            )
        )
    await db_session.commit()
    return ws.id, ctx.id, resource.id, slug, owner


async def _drop_indexed_resource(db_session, ws_id, ctx_id, resource_pk) -> None:
    from sqlalchemy import delete

    from models.memory import MemoryRevision
    from models.resource import IndexerState, Resource, ResourceEvent, ResourceSchema

    await db_session.rollback()
    await db_session.execute(delete(MemoryRevision).where(MemoryRevision.context_id == ctx_id))
    await db_session.execute(delete(Memory).where(Memory.workspace_id == ws_id))
    await db_session.execute(delete(IndexerState).where(IndexerState.context_id == ctx_id))
    for model in (ResourceEvent, ResourceSchema):
        await db_session.execute(delete(model).where(model.resource_pk == resource_pk))
    await db_session.execute(delete(Resource).where(Resource.id == resource_pk))
    await db_session.execute(delete(Context).where(Context.id == ctx_id))
    await db_session.execute(delete(Workspace).where(Workspace.id == ws_id))
    await db_session.commit()


def _db_indexer(db_session):
    with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
        indexer = ResourceIndexer(db_session)
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[0.1] * 512)
    indexer._plan_suspended = AsyncMock(return_value=False)
    indexer._workspace_memory_room = AsyncMock(return_value=10_000)
    return indexer, embedder


async def _db_process(indexer, embedder, slug, ctx_id):
    with (
        patch(
            "services.resource_indexer.resolve_context_routing",
            AsyncMock(return_value=("kagura_memories", embedder)),
        ),
        patch("services.resource_indexer.QuotaService") as quota_cls,
    ):
        quota_cls.return_value.check_memories_per_day = AsyncMock(return_value=(True, None))
        return await indexer.process_incremental(slug, ctx_id)


class TestFailedEventDoesNotPoisonTheBatch:
    """#2218, against the real DB: a DB-level failure in one event (here a
    CHECK violation at its flush) is rolled back on its own; the rest of the
    batch and the offset commit."""

    @pytest.mark.asyncio
    async def test_a_check_violation_in_one_event_leaves_the_rest_committed(self, db_session):
        from models.resource import IndexerState, ResourceEvent

        ws_id, ctx_id, resource_pk, slug, _ = await _seed_indexed_resource(
            db_session,
            [
                # importance outside [0, 1] violates memories.valid_importance.
                {"doc_id": "doc_1", "version": 1, "payload": {"title": "a"}, "importance": 5.0},
                {"doc_id": "doc_2", "version": 1, "payload": {"title": "b"}},
            ],
        )
        try:
            indexer, embedder = _db_indexer(db_session)

            metrics = await _db_process(indexer, embedder, slug, ctx_id)

            assert metrics.errors == 1
            assert metrics.applied_upserts == 1
            assert metrics.reason is None
            db_session.expire_all()
            rows = (
                (await db_session.execute(select(Memory).where(Memory.context_id == ctx_id)))
                .scalars()
                .all()
            )
            assert [m.resource_doc_id for m in rows] == ["doc_2"]
            doc_2_event = (
                await db_session.execute(
                    select(ResourceEvent.id).where(
                        ResourceEvent.resource_pk == resource_pk, ResourceEvent.doc_id == "doc_2"
                    )
                )
            ).scalar_one()
            state = (
                await db_session.execute(
                    select(IndexerState).where(IndexerState.context_id == ctx_id)
                )
            ).scalar_one()
            assert state.last_offset == doc_2_event
        finally:
            await _drop_indexed_resource(db_session, ws_id, ctx_id, resource_pk)


class TestSameVersionReindexKeepsEdits:
    """#2218, against the real DB: a replayed event of the same version keeps
    what a user edited on the row (``context_summary``, ``details.location``)
    and the point follows the row."""

    LOCATION = {"lat": 35.5, "lon": 139.25}

    async def _index_then_replay(self, db_session, *, edit: bool):
        from models.resource import IndexerState
        from services.memory_versions import record_edit, versioned_snapshot

        ws_id, ctx_id, resource_pk, slug, owner = await _seed_indexed_resource(
            db_session, [{"doc_id": "doc_1", "version": 1, "payload": {"title": "Report"}}]
        )
        self.cleanup = (ws_id, ctx_id, resource_pk)
        indexer, embedder = _db_indexer(db_session)
        metrics = await _db_process(indexer, embedder, slug, ctx_id)
        assert metrics.errors == 0, metrics.reason

        row = (
            await db_session.execute(select(Memory).where(Memory.context_id == ctx_id))
        ).scalar_one()
        row_id = row.id
        first_indexed_at = row.details["indexed_at"]
        if edit:
            # The edit the API makes (update_memory): versioned, logged.
            before = versioned_snapshot(row)
            row.context_summary = "what the user wrote"
            row.details = {**row.details, "location": dict(self.LOCATION)}
            row.updated_at = utcnow()
            assert record_edit(db_session, row, before, actor_user_id=owner)
            await db_session.commit()
            await db_session.refresh(row)
            assert row.version == 2
            assert row.location_lat == 35.5

        # Replay: an offset reset re-applies the same version's event.
        state = (
            await db_session.execute(select(IndexerState).where(IndexerState.context_id == ctx_id))
        ).scalar_one()
        state.last_offset = 0
        await db_session.commit()
        indexer, embedder = _db_indexer(db_session)
        metrics = await _db_process(indexer, embedder, slug, ctx_id)
        assert metrics.errors == 0, metrics.reason
        assert metrics.applied_upserts == 1

        db_session.expire_all()
        row = await db_session.get(Memory, row_id)
        payload = indexer.qdrant_client.upsert.await_args.kwargs["points"][0].payload
        return row, payload, first_indexed_at

    @pytest.mark.asyncio
    async def test_an_edited_row_keeps_its_summary_and_location(self, db_session):
        self.cleanup = None
        try:
            row, payload, first_indexed_at = await self._index_then_replay(db_session, edit=True)

            assert row.context_summary == "what the user wrote"
            assert row.details["location"] == self.LOCATION
            assert row.details["indexed_at"] >= first_indexed_at
            assert (row.location_lat, row.location_lon) == (35.5, 139.25)
            assert payload["context_summary"] == normalize_for_search("what the user wrote")
            assert payload["location"] == self.LOCATION
            assert payload["type"] == "resource_data"
        finally:
            if self.cleanup:
                await _drop_indexed_resource(db_session, *self.cleanup)

    @pytest.mark.asyncio
    async def test_an_unedited_row_takes_the_new_text(self, db_session):
        self.cleanup = None
        try:
            row, payload, _ = await self._index_then_replay(db_session, edit=False)

            assert row.version == 1
            assert row.context_summary == "title: Report"
            assert "location" not in row.details
            assert payload["context_summary"] == normalize_for_search("title: Report")
            assert "location" not in payload
        finally:
            if self.cleanup:
                await _drop_indexed_resource(db_session, *self.cleanup)
