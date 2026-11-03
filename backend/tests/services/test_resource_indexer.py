"""Unit tests for ResourceIndexer Qdrant upsert contract.

Issue #324: kagura_memories collection uses named vectors
({"dense": ..., "bm25": ...}); indexer must upsert with dict-keyed
vector, not anonymous.
"""

import asyncio
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import Select, select

from db.qdrant import KAGURA_MEMORIES_BM25_VECTOR_NAME, KAGURA_MEMORIES_VECTOR_NAME
from models.auth import Context, Workspace
from models.memory import Memory
from services.context_routing import resolve_context_routing
from services.resource_indexer import (
    MAX_RECORDED_FAILED_EVENTS,
    ResourceIndexer,
    recorded_failed_events,
)
from utils.datetime import utcnow
from utils.exceptions import QdrantError
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
        self.context = _make_context()
        return self._db_with_existing(None, context=self.context)

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
        context = self.context

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
            self.context,
            "kagura_memories",
            indexer.embedding_service,
        )

        lookups = [
            call.args[0]
            for call in mock_db.execute.call_args_list
            if isinstance(call.args[0], Select)
            and call.args[0].column_descriptions[0]["entity"] is Memory
            and "resource_version =" in str(call.args[0])
        ]
        assert len(lookups) == 1
        lookup_sql = str(lookups[0])
        assert "resource_doc_id" in lookup_sql
        assert "deleted_at IS NULL" in lookup_sql

    @pytest.mark.asyncio
    async def test_apply_upsert_point_carries_the_existing_rows_memory_id(self, indexer, mock_db):
        """#1829: on a re-index the payload memory_id is the row's id, not a
        fresh uuid4() written before the row was looked up (#1808)."""
        existing = _indexed_row()
        mock_db.upsert_existing = existing

        await indexer._apply_upsert(
            _make_event(),
            _make_schema(),
            self.context,
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
            self.context,
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
        context = self.context

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
        context = self.context

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
        context = self.context

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
        context = self.context

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
        context = self.context

        await indexer._apply_upsert(
            event, schema, context, "kagura_memories", indexer.embedding_service
        )

        memory = mock_db.add.call_args.args[0]
        assert set(memory.details.keys()) == {"resource_id", "doc_id", "version", "indexed_at"}
        assert memory.source_uri is None

    @staticmethod
    def _db_with_existing(existing_memory, *, context=None, old_memories=()):
        """Return context locks and each memory query's own result, independent of order."""
        db = AsyncMock()
        db.upsert_context = context if context is not None else _make_context()
        db.upsert_existing = existing_memory
        fresh_context = MagicMock()
        fresh_context.scalar_one_or_none.return_value = db.upsert_context
        existing = MagicMock()
        old_versions = MagicMock()
        old_versions.scalars.return_value.all.return_value = list(old_memories)

        def _side_effect(statement, *_args, **_kwargs):
            if isinstance(statement, Select):
                entity = statement.column_descriptions[0]["entity"]
                if entity is Context:
                    assert statement.compile().params["id_1"] == db.upsert_context.id
                    return fresh_context
                assert entity is Memory, statement
                if "resource_version !=" in str(statement):
                    return old_versions
                existing.scalar_one_or_none.return_value = db.upsert_existing
                return existing
            assert "pg_advisory_xact_lock_shared" in str(statement), statement
            return MagicMock()

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
            event, _make_schema(), db.upsert_context, "kagura_memories", indexer.embedding_service
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
            event, _make_schema(), db.upsert_context, "kagura_memories", indexer.embedding_service
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
            event, _make_schema(), self.context, "kagura_memories", indexer.embedding_service
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
            event, _make_schema(), self.context, "kagura_memories", indexer.embedding_service
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
            event, _make_schema(), self.context, "kagura_memories", indexer.embedding_service
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
        rows = indexer.db.execute.return_value.scalars.return_value
        rows.all.return_value = [_indexed_row()]
        await indexer._apply_delete(
            event, _make_context(), "kagura_memories_qwen3_embedding_4b_2560"
        )
        assert [row.collection for row in indexer._retired] == [
            "kagura_memories_qwen3_embedding_4b_2560"
        ]


class TestApplyDeleteVersionFollowsTheSharedPointRule:
    """#2248: ``version=N`` deletes the version's point by the rule of the
    other delete paths, after the batch commits. The row goes either way."""

    COLLECTION = "kagura_memories"

    def _indexer(self, *rows):
        db = _mock_db()
        result = MagicMock()
        result.scalars.return_value.all.return_value = list(rows)
        db.execute = AsyncMock(return_value=result)
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            return ResourceIndexer(db)

    @staticmethod
    def _version_point():
        from services.resource_indexer import resource_point_id

        return resource_point_id("res_test", "doc_1", 5)

    async def _delete(self, indexer, *, shared=()):
        """``_apply_delete``, then the drain the batch runs after its commit."""
        event = _make_event()
        event.version = 5
        order: list[str] = []
        _, isolated = _drain_session(order, shared=shared)
        self.points = AsyncMock()
        with (
            patch("services.resource_indexer.delete_points_from_qdrant", self.points),
            patch("services.resource_indexer.isolated_session", isolated),
            patch(
                "services.resource_indexer.try_exclude_point_writers",
                AsyncMock(return_value=True),
            ),
            patch("services.resource_indexer.logger") as log,
        ):
            await indexer._apply_delete(event, _make_context(), self.COLLECTION)
            self.points.assert_not_awaited()  # never before the batch commits
            await indexer._drop_retired_points()
        return log

    @pytest.mark.asyncio
    async def test_the_contexts_row_alone_names_the_point_so_it_goes(self):
        row = _indexed_row()
        indexer = self._indexer(row)

        await self._delete(indexer)

        self.points.assert_awaited_once_with([str(self._version_point())], self.COLLECTION)
        indexer.qdrant_client.delete.assert_not_awaited()
        indexer.db.delete.assert_awaited_once_with(row)

    @pytest.mark.asyncio
    async def test_no_row_in_this_context_keeps_the_point(self):
        indexer = self._indexer()

        log = await self._delete(indexer)

        self.points.assert_not_awaited()
        indexer.qdrant_client.delete.assert_not_awaited()
        indexer.db.delete.assert_not_awaited()
        kept = [
            c for c in log.info.call_args_list if c.args[0] == "qdrant_delete_version_point_kept"
        ]
        assert kept[0].kwargs["point_id"] == str(self._version_point())

    @pytest.mark.asyncio
    async def test_another_live_row_keeps_the_point_and_the_row_still_goes(self):
        row = _indexed_row()
        indexer = self._indexer(row)

        log = await self._delete(indexer, shared=[self._version_point()])

        self.points.assert_not_awaited()
        indexer.db.delete.assert_awaited_once_with(row)
        indexer.db.flush.assert_awaited_once()
        kept = [c for c in log.info.call_args_list if c.args[0] == "memory_point_shared_kept"]
        assert kept[0].kwargs["point_id"] == str(self._version_point())

    @pytest.mark.asyncio
    async def test_a_tombstone_beside_the_live_row_does_not_fail_the_delete(self):
        live, tombstone = _indexed_row(), _indexed_row()
        indexer = self._indexer(live, tombstone)

        await self._delete(indexer)

        assert [c.args[0] for c in indexer.db.delete.await_args_list] == [live, tombstone]
        self.points.assert_awaited_once_with([str(self._version_point())], self.COLLECTION)


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


def _after(offset: int, *events: MagicMock) -> list[MagicMock]:
    """``_numbered``, continuing after ``offset`` (the row's ``last_offset``)."""
    numbered = _numbered(*events)
    for event in numbered:
        event.id += offset
    return numbered


def _entry(event_id: int, error_class: str = "RuntimeError") -> dict:
    """A ``failed_events`` entry for event ``event_id`` of ``doc_<event_id>``."""
    return {"event_id": event_id, "doc_id": f"doc_{event_id}", "error_class": error_class}


def test_the_record_reads_only_entries_with_an_integer_event_id():
    """#2477: the batch commit sorts the record by ``event_id``, and the column
    is edited by hand. An entry without an integer id would roll back every
    batch from then on."""
    stored = {
        "failed_events": [
            _entry(7),
            "doc_8",
            None,
            {"event_id": "9", "doc_id": "doc_9"},
            {"doc_id": "doc_10"},
            {"event_id": True, "doc_id": "doc_11"},
        ]
    }

    assert recorded_failed_events(stored) == [_entry(7)]


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
            event,
            _make_schema(),
            indexer.db.upsert_context,
            "kagura_memories",
            indexer.embedding_service,
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
        indexer._get_context = AsyncMock(return_value=db.upsert_context)
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


def _drain_session(order: list[str], *, shared=(), lookup_error=None):
    """A stand-in for ``isolated_session(db)``: the drain's own session (#2248)."""
    session = AsyncMock()
    session.lookups = []

    async def _execute(statement, *_a, **_k):
        order.append("lookup")
        session.lookups.append(str(statement))
        if lookup_error:
            raise lookup_error
        result = MagicMock()
        result.scalars.return_value.all.return_value = list(shared)
        return result

    session.execute = AsyncMock(side_effect=_execute)
    session.commit = AsyncMock(side_effect=lambda: order.append("drain_commit"))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    return session, MagicMock(return_value=cm)


class TestOldVersionCleanupDrainsStrays:
    """#2248: a new version's upsert hard-deletes the old-version row; its
    points go after the batch commits and never on rollback. The version's
    point goes unless another live row names it (one lookup per batch); a row
    that did not own it (a ``remember(external_id)`` row, #2164) also loses
    its own-id point and its chunks, through the re-point drain."""

    COLLECTION = "kagura_memories"

    def _run(self, named, *, commit_error=None, shared=False, lookup_error=None, writers_done=True):
        """process_incremental over one v1 upsert whose doc has a v0 row
        naming ``named`` (``"own"`` = the row id, ``"uuid5"`` = the v0 point)."""
        from services.resource_indexer import resource_point_id

        event = _make_event()
        event.event_metadata = None
        event.created_at = utcnow()
        old = MagicMock()
        old.id = uuid4()
        old.resource_version = 0
        self.version_point = resource_point_id(event.resource_id, event.doc_id, 0)
        old.summary_embedding_id = {"own": old.id, "uuid5": self.version_point}.get(named, named)
        db = TestResourceIndexerNamedVectorUpsert._db_with_existing(None, old_memories=[old])
        self.order: list[str] = []
        self.session, self.isolated = _drain_session(
            self.order,
            shared=[self.version_point] if shared else [],
            lookup_error=lookup_error,
        )

        async def _lock(session):
            assert session is self.session
            self.order.append("lock")
            return writers_done

        self.lock = AsyncMock(side_effect=_lock)
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(db)
        indexer.embedding_service = MagicMock()
        indexer.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)
        state = MagicMock(last_offset=0, last_run_at=None, metrics=None)
        indexer._get_or_create_state = AsyncMock(return_value=state)
        indexer._fetch_events = AsyncMock(return_value=[event])
        indexer._get_latest_schema = AsyncMock(return_value=_make_schema())
        indexer._get_context = AsyncMock(return_value=db.upsert_context)
        indexer._existing_resource_doc_ids = AsyncMock(return_value={event.doc_id})
        indexer._plan_suspended = AsyncMock(return_value=False)

        db.commit = AsyncMock(side_effect=commit_error or (lambda: self.order.append("commit")))
        db.rollback = AsyncMock()
        self.chunks = AsyncMock(side_effect=lambda ids, _c: self.order.append(f"chunks:{ids[0]}"))
        self.points = AsyncMock(
            side_effect=lambda ids, _c: self.order.append(f"points:{','.join(ids)}")
        )
        return indexer, old

    async def _process(self, indexer):
        routing = AsyncMock(return_value=(self.COLLECTION, indexer.embedding_service))
        with (
            patch("services.resource_indexer.resolve_context_routing", routing),
            patch("services.resource_lookup.resolve_resource_pk", AsyncMock(return_value=1)),
            patch("services.resource_indexer.delete_chunk_points", self.chunks),
            patch("services.resource_indexer.delete_points_from_qdrant", self.points),
            patch("services.resource_indexer.isolated_session", self.isolated),
            patch("services.resource_indexer.try_exclude_point_writers", self.lock),
            patch("services.resource_indexer.logger") as log,
        ):
            metrics = await indexer.process_incremental("res_test", uuid4())
        return metrics, log

    @pytest.mark.asyncio
    async def test_an_unowned_old_row_drops_its_own_point_chunks_and_version_point(self):
        indexer, old = self._run("own")

        metrics, log = await self._process(indexer)

        assert metrics.errors == 0
        # Batch commit, the re-point drain, then, in the drain's own session
        # under the writer lock, one lookup and one delete for the batch.
        assert self.order == [
            "commit",
            f"chunks:{old.id}",
            f"points:{old.id}",
            "lock",
            "lookup",
            f"points:{self.version_point}",
            "drain_commit",
        ]
        self.chunks.assert_awaited_once_with([old.id], self.COLLECTION)
        self.points.assert_awaited_with([str(self.version_point)], self.COLLECTION)
        assert "deleted_at IS NULL" in self.session.lookups[0]
        self.isolated.assert_called_once_with(indexer.db)
        indexer.db.commit.assert_awaited_once()
        indexer.qdrant_client.delete.assert_not_awaited()
        assert indexer._retired == [] and indexer._repointed == []
        events = [c.args[0] for c in log.info.call_args_list]
        assert "old_versions_cleaned" in events
        assert "old_version_points_dropped" in events

    @pytest.mark.asyncio
    async def test_an_owned_old_row_drops_only_its_version_point(self):
        indexer, _ = self._run("uuid5")

        await self._process(indexer)

        assert self.order == [
            "commit",
            "lock",
            "lookup",
            f"points:{self.version_point}",
            "drain_commit",
        ]
        self.chunks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_row_without_a_point_drops_chunks_and_own_id(self):
        indexer, old = self._run(None)

        await self._process(indexer)

        assert self.order[:3] == ["commit", f"chunks:{old.id}", f"points:{old.id}"]
        assert self.order[-2:] == [f"points:{self.version_point}", "drain_commit"]

    @pytest.mark.asyncio
    async def test_another_named_point_is_kept(self):
        other = uuid4()
        indexer, old = self._run(other)

        _, log = await self._process(indexer)

        assert all(str(other) not in step for step in self.order)
        assert self.order[1] == f"chunks:{old.id}"
        kept = [
            c
            for c in log.info.call_args_list
            if c.args[0] == "resource_repoint_previous_point_kept"
        ]
        assert kept and kept[0].kwargs["point_id"] == str(other)

    @pytest.mark.asyncio
    async def test_a_shared_version_point_is_kept(self):
        indexer, old = self._run("uuid5", shared=True)

        metrics, log = await self._process(indexer)

        assert metrics.errors == 0
        assert self.order == ["commit", "lock", "lookup", "drain_commit"]
        self.points.assert_not_awaited()
        kept = [c for c in log.info.call_args_list if c.args[0] == "memory_point_shared_kept"]
        assert kept[0].kwargs == {"memory_id": str(old.id), "point_id": str(self.version_point)}

    @pytest.mark.asyncio
    async def test_a_rolled_back_batch_deletes_nothing(self):
        indexer, _ = self._run("own", commit_error=RuntimeError("commit failed"))

        metrics, _ = await self._process(indexer)

        assert metrics.errors == 1
        indexer.db.rollback.assert_awaited_once()
        self.chunks.assert_not_awaited()
        self.points.assert_not_awaited()
        self.isolated.assert_not_called()
        assert indexer._retired == [] and indexer._repointed == []

    @pytest.mark.asyncio
    async def test_a_failed_lookup_keeps_the_points_and_spares_the_batch_session(self):
        indexer, _ = self._run("uuid5", lookup_error=RuntimeError("db gone"))

        metrics, log = await self._process(indexer)

        assert metrics.errors == 0
        assert self.order == ["commit", "lock", "lookup"]
        self.points.assert_not_awaited()
        indexer.db.rollback.assert_not_awaited()
        warned = [c.args[0] for c in log.warning.call_args_list]
        assert "old_version_point_lookup_failed" in warned

    @pytest.mark.asyncio
    async def test_open_point_writers_leave_the_points_to_the_sweep(self):
        indexer, _ = self._run("uuid5", writers_done=False)

        metrics, log = await self._process(indexer)

        assert metrics.errors == 0
        assert self.order == ["commit", "lock"]
        self.points.assert_not_awaited()
        self.lock.assert_awaited_once()
        deferred = [
            c for c in log.info.call_args_list if c.args[0] == "old_version_points_deferred"
        ]
        assert deferred[0].kwargs == {"rows": 1}

    @pytest.mark.asyncio
    async def test_a_failed_chunk_delete_still_drops_the_version_point(self):
        indexer, _ = self._run("own")
        self.chunks.side_effect = RuntimeError("qdrant down")

        metrics, log = await self._process(indexer)

        assert metrics.errors == 0
        self.points.assert_awaited_once_with([str(self.version_point)], self.COLLECTION)
        warned = [c.args[0] for c in log.warning.call_args_list]
        assert "resource_repoint_cleanup_failed" in warned

    @pytest.mark.asyncio
    async def test_a_failed_version_point_delete_is_logged_and_the_run_completes(self):
        indexer, _ = self._run("uuid5")
        self.points.side_effect = RuntimeError("qdrant down")

        metrics, log = await self._process(indexer)

        assert metrics.errors == 0
        warned = [
            c for c in log.warning.call_args_list if c.args[0] == "old_version_point_cleanup_failed"
        ]
        assert warned[0].kwargs == {
            "points": 1,
            "collection": self.COLLECTION,
            "error_class": "RuntimeError",
        }
        assert indexer._retired == []

    @pytest.mark.asyncio
    async def test_a_stalled_vector_store_is_cut_off_and_the_lock_released(self):
        """#2478: the drain holds the point writers out, so a delete that
        never returns must not hold them past the bound; what it did not
        delete is left to the orphan sweep."""
        indexer, _ = self._run("uuid5")

        async def _stall(ids, _collection):
            self.order.append(f"points:{','.join(ids)}")
            await asyncio.Event().wait()

        self.points.side_effect = _stall

        started = time.monotonic()
        with patch("services.resource_indexer.RETIRED_POINT_DELETE_TIMEOUT_SECONDS", 0.05):
            metrics, log = await self._process(indexer)
        elapsed = time.monotonic() - started

        assert elapsed < 2.0
        assert metrics.errors == 0
        assert metrics.batch_committed is True
        # No drain commit: the cancelled session closes, which ends its
        # transaction and with it the lock.
        assert self.order == ["commit", "lock", "lookup", f"points:{self.version_point}"]
        session_cm = self.isolated.return_value
        session_cm.__aexit__.assert_awaited_once()
        assert session_cm.__aexit__.await_args.args[0] is asyncio.CancelledError
        timed_out = [
            c
            for c in log.warning.call_args_list
            if c.args[0] == "old_version_point_drain_timed_out"
        ]
        assert len(timed_out) == 1
        assert timed_out[0].kwargs == {
            "timeout_seconds": 0.05,
            "points_dropped": 0,
            "points_left": 1,
        }
        warned = [c.args[0] for c in log.warning.call_args_list]
        assert "old_version_point_lookup_failed" not in warned
        assert indexer._retired == []


def _retired_rows(*collections: str) -> list:
    from services.resource_indexer import RetiredVersionRow

    return [RetiredVersionRow(uuid4(), uuid4(), collection) for collection in collections]


class TestRetiredPointDrainIsBounded:
    """#2478: ``_drop_retired_points`` holds the point-writer lock exclusively
    from its lookup to its last delete, and every new point writer queues
    behind it. ``RETIRED_POINT_DELETE_TIMEOUT_SECONDS`` bounds the whole
    block, as ``FORGET_POINT_DELETE_TIMEOUT_SECONDS`` bounds forget's."""

    async def _drain(self, rows, delete, *, timeout=2.0, shared=(), lookup_error=None):
        order: list[str] = []
        _, isolated = _drain_session(order, shared=shared, lookup_error=lookup_error)
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(_mock_db())
        indexer._retired = list(rows)
        with (
            patch("services.resource_indexer.isolated_session", isolated),
            patch(
                "services.resource_indexer.try_exclude_point_writers", AsyncMock(return_value=True)
            ),
            patch("services.resource_indexer.delete_points_from_qdrant", delete),
            patch("services.resource_indexer.RETIRED_POINT_DELETE_TIMEOUT_SECONDS", timeout),
            patch("services.resource_indexer.logger") as log,
        ):
            started = time.monotonic()
            await indexer._drop_retired_points()
            elapsed = time.monotonic() - started
        return order, isolated.return_value, log, elapsed

    @pytest.mark.asyncio
    async def test_the_normal_path_deletes_every_collection_and_commits(self):
        rows = _retired_rows("kagura_memories", "kagura_memories_other")
        deleted: list[tuple[list[str], str]] = []

        async def _delete(ids, collection):
            deleted.append((list(ids), collection))

        order, session_cm, log, _ = await self._drain(rows, _delete)

        assert deleted == [
            ([str(rows[0].version_point)], "kagura_memories"),
            ([str(rows[1].version_point)], "kagura_memories_other"),
        ]
        assert order == ["lookup", "drain_commit"]
        session_cm.__aexit__.assert_awaited_once()
        assert session_cm.__aexit__.await_args.args[0] is None
        dropped = [c for c in log.info.call_args_list if c.args[0] == "old_version_points_dropped"]
        assert len(dropped) == 2
        log.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stall_after_one_collection_counts_what_went_and_what_is_left(self):
        rows = _retired_rows("kagura_memories", "kagura_memories_other", "kagura_memories_other")
        deleted: list[str] = []

        async def _delete(ids, collection):
            if collection == "kagura_memories_other":
                await asyncio.Event().wait()
            deleted.extend(ids)

        order, session_cm, log, elapsed = await self._drain(rows, _delete, timeout=0.05)

        assert elapsed < 2.0
        assert deleted == [str(rows[0].version_point)]
        assert order == ["lookup"]
        assert session_cm.__aexit__.await_args.args[0] is asyncio.CancelledError
        timed_out = [
            c
            for c in log.warning.call_args_list
            if c.args[0] == "old_version_point_drain_timed_out"
        ]
        assert [c.kwargs for c in timed_out] == [
            {"timeout_seconds": 0.05, "points_dropped": 1, "points_left": 2}
        ]

    @pytest.mark.asyncio
    async def test_a_shared_point_is_not_counted_as_left(self):
        rows = _retired_rows("kagura_memories", "kagura_memories")

        async def _stall(_ids, _collection):
            await asyncio.Event().wait()

        _, _, log, _ = await self._drain(rows, _stall, timeout=0.05, shared=[rows[0].version_point])

        timed_out = [
            c
            for c in log.warning.call_args_list
            if c.args[0] == "old_version_point_drain_timed_out"
        ]
        assert [c.kwargs for c in timed_out] == [
            {"timeout_seconds": 0.05, "points_dropped": 0, "points_left": 1}
        ]

    @pytest.mark.asyncio
    async def test_a_timeout_error_from_inside_is_a_failure_not_the_bound(self):
        """A ``TimeoutError`` the DB driver raises (a connect timeout) is not
        the drain's bound expiring: it is logged as the lookup failure it is,
        with its class, not as a stalled vector store."""
        rows = _retired_rows("kagura_memories")
        delete = AsyncMock()

        _, _, log, _ = await self._drain(rows, delete, lookup_error=TimeoutError())

        delete.assert_not_awaited()
        warned = [(c.args[0], c.kwargs) for c in log.warning.call_args_list]
        assert warned == [
            ("old_version_point_lookup_failed", {"rows": 1, "error_class": "TimeoutError"})
        ]


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

    async def _process(self, indexer, chunks=None, points=None, failed_events=None):
        routing = AsyncMock(return_value=(self.COLLECTION, MagicMock()))
        kwargs = {} if failed_events is None else {"failed_events": failed_events}
        with (
            patch("services.resource_indexer.resolve_context_routing", routing),
            patch("services.resource_indexer.QuotaService") as quota_cls,
            patch("services.resource_indexer.delete_chunk_points", chunks or AsyncMock()),
            patch("services.resource_indexer.delete_points_from_qdrant", points or AsyncMock()),
        ):
            quota_cls.return_value.check_memories_per_day = AsyncMock(return_value=(True, None))
            return await indexer.process_incremental("res_test", uuid4(), **kwargs)

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
    async def test_a_failed_event_is_recorded_with_its_class_only(self):
        """#2254: the offset moves past it, so this is the rewind target."""
        events = _numbered(_upsert("doc_1"), _upsert("doc_2"), _upsert("doc_3"))
        indexer = self._indexer(events)
        indexer._apply_upsert.side_effect = [None, RuntimeError("secret payload"), None]

        metrics = await self._process(indexer)

        expected = [{"event_id": events[1].id, "doc_id": "doc_2", "error_class": "RuntimeError"}]
        assert metrics.failed_events == expected
        assert self.state.metrics["failed_events"] == expected
        assert "secret payload" not in str(self.state.metrics)
        assert metrics.batch_committed is True

    @pytest.mark.asyncio
    async def test_a_wrapped_failure_is_recorded_under_its_cause(self):
        """``_apply_delete`` turns a Postgres failure into a QdrantError too."""
        indexer = self._indexer(_numbered(_upsert("doc_1")))

        async def _apply(*_a):
            try:
                raise ValueError("flush failed")
            except ValueError as e:
                raise QdrantError("Failed to delete document") from e

        indexer._apply_upsert.side_effect = _apply

        metrics = await self._process(indexer)

        assert metrics.failed_events[0]["error_class"] == "ValueError"

    @pytest.mark.asyncio
    async def test_the_record_keeps_the_oldest_events_up_to_the_cap(self):
        events = _numbered(*[_upsert(f"doc_{i}") for i in range(25)])
        indexer = self._indexer(events)
        indexer._apply_upsert.side_effect = RuntimeError("boom")

        metrics = await self._process(indexer)

        assert metrics.errors == 25
        recorded = self.state.metrics["failed_events"]
        assert len(recorded) == MAX_RECORDED_FAILED_EVENTS == 20
        assert [e["event_id"] for e in recorded] == [e.id for e in events[:20]]

    @pytest.mark.asyncio
    async def test_a_clean_batch_commits_an_empty_record(self):
        indexer = self._indexer(_numbered(_upsert("doc_1")))

        metrics = await self._process(indexer)

        assert metrics.failed_events == []
        assert self.state.metrics["failed_events"] == []
        assert metrics.batch_committed is True

    @pytest.mark.asyncio
    async def test_a_rolled_back_batch_is_not_marked_committed(self):
        """The job then keeps the row's record: see TestFailedEventsSurviveTheJob."""
        indexer = self._indexer(_numbered(_upsert("doc_1")))
        indexer.db.commit.side_effect = RuntimeError("commit failed")

        metrics = await self._process(indexer)

        indexer.db.rollback.assert_awaited_once()
        assert metrics.batch_committed is False

    @pytest.mark.asyncio
    async def test_a_run_with_nothing_pending_is_not_marked_committed(self):
        indexer = self._indexer([])

        metrics = await self._process(indexer)

        assert metrics.reason == "no_pending_events"
        assert metrics.batch_committed is False

    @pytest.mark.asyncio
    async def test_a_clean_batch_keeps_the_record_it_was_given(self):
        """#2477: the skipped event was not re-applied, so only an operator
        rewind or clear takes it off the record."""
        indexer = self._indexer(_after(10, _upsert("doc_11"), _upsert("doc_12")))
        self.state.last_offset = 10

        metrics = await self._process(indexer, failed_events=[_entry(7)])

        assert metrics.errors == 0
        assert metrics.failed_events == [_entry(7)]
        assert self.state.metrics["failed_events"] == [_entry(7)]

    @pytest.mark.asyncio
    async def test_the_batch_commit_writes_the_merged_record(self):
        """#2477: the record goes into the batch's own commit, not into a later
        one in the job that a process can die before."""
        indexer = self._indexer(_after(10, _upsert("doc_11"), _upsert("doc_12")))
        self.state.last_offset = 10
        indexer._apply_upsert.side_effect = [None, RuntimeError("boom")]
        committed = []
        indexer.db.commit.side_effect = lambda: committed.append(
            self.state.metrics["failed_events"]
        )

        metrics = await self._process(indexer, failed_events=[_entry(7)])

        assert committed == [[_entry(7), _entry(12)]]
        assert metrics.failed_events == [_entry(7), _entry(12)]

    @pytest.mark.asyncio
    async def test_a_replay_drops_the_entries_it_ran_again_and_keeps_the_rest(self):
        """#2477: after a rewind to 4 the batch runs 5 to 8 again. 3 is at or
        below the target and 20 past this batch, so both stay; 5 succeeded and
        leaves; 8 failed again and is listed once, with its new class."""
        events = _after(4, *[_upsert(f"doc_{i}") for i in range(5, 9)])
        indexer = self._indexer(events)
        self.state.last_offset = 4
        indexer._apply_upsert.side_effect = [None, None, None, ValueError("again")]

        metrics = await self._process(
            indexer, failed_events=[_entry(3), _entry(5), _entry(8), _entry(20)]
        )

        expected = [_entry(3), _entry(8, "ValueError"), _entry(20)]
        assert metrics.failed_events == expected
        assert self.state.metrics["failed_events"] == expected

    @pytest.mark.asyncio
    async def test_the_merged_record_keeps_the_oldest_events_up_to_the_cap(self):
        indexer = self._indexer(_after(100, _upsert("doc_101")))
        self.state.last_offset = 100
        indexer._apply_upsert.side_effect = RuntimeError("boom")
        recorded = [_entry(i) for i in range(1, MAX_RECORDED_FAILED_EVENTS + 1)]

        metrics = await self._process(indexer, failed_events=recorded)

        assert metrics.errors == 1
        assert self.state.metrics["failed_events"] == recorded

    @pytest.mark.asyncio
    async def test_a_full_record_lists_an_event_that_failed_again_once(self):
        """#2477: the entry the batch ran again gives way to its new outcome
        before the cap is applied, so it is neither counted twice nor cut."""
        indexer = self._indexer(_after(100, _upsert("doc_101"), _upsert("doc_102")))
        self.state.last_offset = 100
        indexer._apply_upsert.side_effect = [ValueError("again"), RuntimeError("new")]
        recorded = [_entry(i) for i in range(1, MAX_RECORDED_FAILED_EVENTS)] + [_entry(101)]

        metrics = await self._process(indexer, failed_events=recorded)

        assert metrics.failed_events == [*recorded[:-1], _entry(101, "ValueError")]
        assert self.state.metrics["failed_events"] == metrics.failed_events

    @pytest.mark.asyncio
    async def test_a_failure_at_the_end_of_a_batch_leaves_once_the_next_run_applies_it(self):
        """#2477: the offset stays below a trailing failure, so the next run
        runs it again; once it succeeds it is off the record."""
        first = self._indexer(_after(10, _upsert("doc_11"), _upsert("doc_12")))
        self.state.last_offset = 10
        first._apply_upsert.side_effect = [None, RuntimeError("boom")]

        after_first = await self._process(first, failed_events=[_entry(7)])

        assert self.state.last_offset == 11
        assert after_first.failed_events == [_entry(7), _entry(12)]

        second = self._indexer(_after(11, _upsert("doc_12"), _upsert("doc_13")))
        self.state.last_offset = 11

        after_second = await self._process(second, failed_events=after_first.failed_events)

        assert self.state.last_offset == 13
        assert after_second.failed_events == [_entry(7)]
        assert self.state.metrics["failed_events"] == [_entry(7)]

    @pytest.mark.asyncio
    async def test_a_rolled_back_batch_returns_the_record_it_was_given(self):
        """The offset went back with the batch, so the batch runs again: its
        own failures are not skipped, and 11, which it ran, stays listed."""
        indexer = self._indexer(_after(10, _upsert("doc_11"), _upsert("doc_12")))
        self.state.last_offset = 10
        indexer._apply_upsert.side_effect = [None, RuntimeError("boom")]
        indexer.db.commit.side_effect = RuntimeError("commit failed")

        metrics = await self._process(indexer, failed_events=[_entry(7), _entry(11)])

        assert metrics.batch_committed is False
        assert metrics.failed_events == [_entry(7), _entry(11)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reason", ["no_pending_events", "plan_suspended", "resource_entity_missing"]
    )
    async def test_a_run_without_a_batch_returns_the_record_it_was_given(self, reason):
        """The job stores what the run returns on every path (#2477)."""
        events = [] if reason == "no_pending_events" else _after(10, _upsert("doc_11"))
        indexer = self._indexer(events)
        indexer._plan_suspended.return_value = reason == "plan_suspended"
        pk = None if reason == "resource_entity_missing" else uuid4()

        with patch("services.resource_lookup.resolve_resource_pk", AsyncMock(return_value=pk)):
            metrics = await self._process(indexer, failed_events=[_entry(7)])

        assert metrics.reason == reason
        assert metrics.failed_events == [_entry(7)]
        indexer._apply_upsert.assert_not_awaited()

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
        db = TestResourceIndexerNamedVectorUpsert._db_with_existing(None, old_memories=[old])
        flushes = 0

        async def _flush():
            nonlocal flushes
            flushes += 1
            if flushes == 2:  # the row's flush passes, the cleanup's fails
                raise RuntimeError("cleanup flush failed")

        db.flush = AsyncMock(side_effect=_flush)
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
                event, _make_schema(), db.upsert_context, self.COLLECTION, indexer.embedding_service
            )

        db.begin_nested.assert_called_once()
        assert cleanup.__aexit__.await_args.args[0] is RuntimeError
        db.add.assert_called_once()
        warned = [c.args[0] for c in log.warning.call_args_list]
        assert "old_version_cleanup_failed" in warned
        # The old row was rolled back, so it keeps its point.
        indexer.qdrant_client.delete.assert_not_awaited()
        assert indexer._retired == []

    @pytest.mark.asyncio
    async def test_old_points_are_queued_not_deleted_inside_the_event(self):
        """#2248: the event only queues the old row's points; the batch drains
        them after its commit (``TestOldVersionCleanupDrainsStrays``)."""
        from services.resource_indexer import RetiredVersionRow, resource_point_id

        old = MagicMock()
        old.id = uuid4()
        old.resource_version = 0
        old.summary_embedding_id = old.id
        db = TestResourceIndexerNamedVectorUpsert._db_with_existing(None, old_memories=[old])
        db.begin_nested = MagicMock(side_effect=lambda: _savepoint())
        with patch("services.resource_indexer.get_qdrant_client", return_value=AsyncMock()):
            indexer = ResourceIndexer(db)
        indexer.embedding_service = MagicMock()
        indexer.embedding_service.embed = AsyncMock(return_value=[0.1] * 512)
        event = _make_event()
        event.event_metadata = None

        with (
            patch("services.resource_indexer.delete_chunk_points") as chunks,
            patch("services.resource_indexer.delete_points_from_qdrant") as points,
        ):
            await indexer._apply_upsert(
                event, _make_schema(), db.upsert_context, self.COLLECTION, indexer.embedding_service
            )

        db.delete.assert_awaited_once_with(old)
        indexer.qdrant_client.delete.assert_not_awaited()
        chunks.assert_not_called()
        points.assert_not_called()
        assert indexer._retired == [
            RetiredVersionRow(
                memory_id=old.id,
                version_point=resource_point_id(event.resource_id, event.doc_id, 0),
                collection=self.COLLECTION,
            )
        ]
        # It named its own id, not the version's point: the re-point drain
        # drops that point and its chunks.
        assert indexer._repointed == [(old.id, old.id, self.COLLECTION)]

    @pytest.mark.asyncio
    async def test_a_failed_events_retired_rows_are_dropped(self):
        from services.resource_indexer import RetiredVersionRow

        events = _numbered(_upsert("doc_1"), _upsert("doc_2"))
        indexer = self._indexer(events)
        kept, dropped = uuid4(), uuid4()

        kept_point, dropped_point = uuid4(), uuid4()

        async def _apply(event, *_a):
            if event.doc_id == "doc_1":
                indexer._retired.append(RetiredVersionRow(kept, kept_point, self.COLLECTION))
                return
            indexer._retired.append(RetiredVersionRow(dropped, dropped_point, self.COLLECTION))
            raise RuntimeError("release failed after the cleanup")

        indexer._apply_upsert.side_effect = _apply
        # The drain's own session: the lookup finds no other row.
        _, isolated = _drain_session([])
        points = AsyncMock()

        with (
            patch("services.resource_indexer.isolated_session", isolated),
            patch(
                "services.resource_indexer.try_exclude_point_writers",
                AsyncMock(return_value=True),
            ),
        ):
            metrics = await self._process(indexer, AsyncMock(), points)

        assert metrics.errors == 1
        points.assert_awaited_once_with([str(kept_point)], self.COLLECTION)
        assert indexer._retired == []


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


async def _db_process(indexer, embedder, slug, ctx_id, **kwargs):
    with (
        patch(
            "services.resource_indexer.resolve_context_routing",
            AsyncMock(return_value=("kagura_memories", embedder)),
        ),
        patch("services.resource_indexer.QuotaService") as quota_cls,
    ):
        quota_cls.return_value.check_memories_per_day = AsyncMock(return_value=(True, None))
        return await indexer.process_incremental(slug, ctx_id, **kwargs)


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


class TestTheBatchCommitHoldsTheRecord:
    """#2477, against the real DB: the batch commit itself writes the row's
    record of skipped events merged with the batch's own, so a process that
    dies before the job's later commit keeps both."""

    @pytest.mark.asyncio
    async def test_the_row_holds_the_merged_record_once_the_batch_commits(self, db_session):
        from sqlalchemy import func

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
            first = (
                await db_session.execute(
                    select(func.min(ResourceEvent.id)).where(
                        ResourceEvent.resource_pk == resource_pk
                    )
                )
            ).scalar_one()
            earlier = {"event_id": first - 1, "doc_id": "doc_0", "error_class": "QdrantError"}
            db_session.add(
                IndexerState(
                    resource_pk=resource_pk,
                    resource_id=slug,
                    context_id=ctx_id,
                    last_offset=first - 1,
                    job_status="running",
                    metrics={"failed_events": [earlier]},
                )
            )
            await db_session.commit()
            indexer, embedder = _db_indexer(db_session)

            metrics = await _db_process(indexer, embedder, slug, ctx_id, failed_events=[earlier])

            assert metrics.batch_committed is True
            stored = (
                await db_session.execute(
                    select(IndexerState.metrics).where(IndexerState.context_id == ctx_id)
                )
            ).scalar_one()
            assert [(e["event_id"], e["doc_id"]) for e in stored["failed_events"]] == [
                (first - 1, "doc_0"),
                (first, "doc_1"),
            ]
        finally:
            await _drop_indexed_resource(db_session, ws_id, ctx_id, resource_pk)


class TestSameVersionReindexKeepsEdits:
    """#2218, against the real DB: a replayed event of the same version keeps
    what a user edited on the row (``context_summary``, ``details.location``)
    and the point follows the row."""

    LOCATION = {"lat": 35.5, "lon": 139.25}
    # #2254: the details keys the indexer never takes from worker lineage.
    ROW_AUTHORED = {
        "external_blob": {"backend": "r2", "ref": "r2://bucket/report.pdf"},
        "trigger": {"from": "2027-01-01T00:00:00", "until": "2027-01-02T00:00:00"},
        "location": {"lat": 35.5, "lon": 139.25},
        "tool_trigger": {"tool": "Bash", "on": "pre", "action": "warn"},
    }

    async def _index_then_replay(self, db_session, *, edit: bool, edit_row=None, event=None):
        from models.resource import IndexerState
        from services.memory_versions import record_edit, versioned_snapshot

        ws_id, ctx_id, resource_pk, slug, owner = await _seed_indexed_resource(
            db_session,
            [event or {"doc_id": "doc_1", "version": 1, "payload": {"title": "Report"}}],
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
            if edit_row is None:
                row.context_summary = "what the user wrote"
                row.details = {**row.details, "location": dict(self.LOCATION)}
            else:
                edit_row(row)
            row.updated_at = utcnow()
            assert record_edit(db_session, row, before, actor_user_id=owner)
            await db_session.commit()
            await db_session.refresh(row)
            assert row.version == 2
            if edit_row is None:
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

    @pytest.mark.asyncio
    async def test_reserved_details_keys_and_importance_survive_a_replay(self, db_session):
        """#2254: the four row-authored details keys and ``importance`` stay;
        any other key comes from the event (worker lineage stays authoritative)."""
        self.cleanup = None

        def _edit(row):
            row.details = {
                **row.details,
                **self.ROW_AUTHORED,
                "channel_id": "C-edited",
                "note": "a key the event does not carry",
            }
            row.importance = 0.95

        try:
            row, payload, _ = await self._index_then_replay(
                db_session,
                edit=True,
                edit_row=_edit,
                event={
                    "doc_id": "doc_1",
                    "version": 1,
                    "payload": {"title": "Report"},
                    "event_metadata": {"memory_details": {"channel_id": "C-worker"}},
                },
            )

            for key, value in self.ROW_AUTHORED.items():
                assert row.details[key] == value, key
            assert row.details["channel_id"] == "C-worker"
            assert "note" not in row.details
            assert row.importance == 0.95
            assert row.trigger_from == "2027-01-01T00:00:00"
            assert row.external_blob_ref == "r2://bucket/report.pdf"
            assert payload["location"] == self.LOCATION
        finally:
            if self.cleanup:
                await _drop_indexed_resource(db_session, *self.cleanup)

    @pytest.mark.asyncio
    async def test_a_remembered_rows_first_match_keeps_its_importance_and_keys(self, db_session):
        """#2254: a ``remember(external_id)`` row matched by natural key keeps
        the importance and the row-authored keys it was written with."""
        ws_id, ctx_id, resource_pk, slug, owner = await _seed_indexed_resource(
            db_session,
            [{"doc_id": "doc_1", "version": 1, "payload": {"title": "Report"}}],
        )
        try:
            row = Memory(
                id=uuid4(),
                user_id=owner,
                workspace_id=ws_id,
                context_id=ctx_id,
                summary="a remembered external document",
                content='{"title": "Report"}',
                details={
                    "resource_id": slug,
                    "doc_id": "doc_1",
                    "version": 1,
                    "external_blob": self.ROW_AUTHORED["external_blob"],
                },
                type="fact",
                importance=0.9,
                client="pytest",
                embedding_status="success",
            )
            row.summary_embedding_id = row.id
            db_session.add(row)
            await db_session.commit()
            row_id = row.id
            indexer, embedder = _db_indexer(db_session)

            # The re-point drain's deletes (#2164) are not under test here.
            with (
                patch("services.resource_indexer.delete_chunk_points", AsyncMock()),
                patch("services.resource_indexer.delete_points_from_qdrant", AsyncMock()),
            ):
                metrics = await _db_process(indexer, embedder, slug, ctx_id)

            assert metrics.errors == 0, metrics.reason
            assert metrics.applied_upserts == 1
            db_session.expire_all()
            row = await db_session.get(Memory, row_id)
            assert row.importance == 0.9
            assert row.details["external_blob"] == self.ROW_AUTHORED["external_blob"]
            assert row.source_type == "connector"
        finally:
            await _drop_indexed_resource(db_session, ws_id, ctx_id, resource_pk)


class TestOldVersionPointSharedAfterSlugRelease:
    """#2248, against the real DB: a context that released its slug keeps rows
    naming the slug's version points. When the context that took the slug over
    retires that version, the point stays for the other context's live row."""

    @pytest.mark.asyncio
    async def test_the_drain_keeps_a_point_another_contexts_row_names(self, db_session):
        from sqlalchemy import delete

        from services.resource_indexer import resource_point_id

        ws_id, ctx_id, resource_pk, slug, owner = await _seed_indexed_resource(
            db_session,
            [
                {"doc_id": "doc_1", "version": 1, "payload": {"title": "v1"}},
                {"doc_id": "doc_1", "version": 2, "payload": {"title": "v2"}},
            ],
        )
        v1_point = resource_point_id(slug, "doc_1", 1)
        # The context that served the slug before (unpublished since): its
        # row still names the v1 point.
        released = Context(
            id=uuid4(),
            workspace_id=ws_id,
            name=f"ctx-{uuid4().hex[:8]}",
            created_by=owner,
            is_private=False,
        )
        db_session.add(released)
        await db_session.flush()
        kept_row = Memory(
            id=uuid4(),
            user_id=owner,
            workspace_id=ws_id,
            context_id=released.id,
            summary="indexed before the slug moved",
            content='{"title": "v1"}',
            details={"resource_id": slug, "doc_id": "doc_1", "version": 1},
            type="resource_data",
            source="resource_ingest",
            client="resource_indexer",
            embedding_status="success",
            summary_embedding_id=v1_point,
        )
        db_session.add(kept_row)
        await db_session.commit()
        released_id, kept_row_id = released.id, kept_row.id
        try:
            indexer, embedder = _db_indexer(db_session)
            deleted: list[list[str]] = []

            async def _delete(point_ids, _collection):
                deleted.append(list(point_ids))

            with (
                patch("services.resource_indexer.delete_points_from_qdrant", _delete),
                patch("services.resource_indexer.delete_chunk_points", AsyncMock()),
                patch("services.resource_indexer.logger") as log,
            ):
                metrics = await _db_process(indexer, embedder, slug, ctx_id)

            assert metrics.errors == 0, metrics.reason
            assert metrics.applied_upserts == 2
            assert str(v1_point) not in [p for batch in deleted for p in batch]
            kept = [c for c in log.info.call_args_list if c.args[0] == "memory_point_shared_kept"]
            assert kept and kept[0].kwargs["point_id"] == str(v1_point)
            db_session.expire_all()
            assert await db_session.get(Memory, kept_row_id) is not None
        finally:
            await db_session.rollback()
            await db_session.execute(delete(Memory).where(Memory.context_id == released_id))
            await db_session.execute(delete(Context).where(Context.id == released_id))
            await db_session.commit()
            await _drop_indexed_resource(db_session, ws_id, ctx_id, resource_pk)


class TestApplyDeleteVersionAgainstTheDb:
    """#2248, against the real DB: ``version=N`` keeps the version's point
    while another context's live row names it, and deletes it otherwise, with
    a tombstone of the same version beside the row."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("other_row_names_it", [True, False])
    async def test_the_point_goes_only_when_no_other_live_row_names_it(
        self, db_session, other_row_names_it
    ):
        from sqlalchemy import delete

        from services.resource_indexer import resource_point_id

        ws_id, ctx_id, resource_pk, slug, owner = await _seed_indexed_resource(db_session, [])
        point = resource_point_id(slug, "doc_1", 1)
        other = Context(
            id=uuid4(),
            workspace_id=ws_id,
            name=f"ctx-{uuid4().hex[:8]}",
            created_by=owner,
            is_private=False,
        )
        db_session.add(other)
        await db_session.flush()

        def _row(context_id):
            return Memory(
                id=uuid4(),
                user_id=owner,
                workspace_id=ws_id,
                context_id=context_id,
                summary="an indexed document",
                content='{"title": "v1"}',
                details={"resource_id": slug, "doc_id": "doc_1", "version": 1},
                type="resource_data",
                source="resource_ingest",
                client="resource_indexer",
                embedding_status="success",
                summary_embedding_id=point,
            )

        row, other_row, tombstone = _row(ctx_id), _row(other.id), _row(ctx_id)
        tombstone.deleted_at = utcnow()
        db_session.add_all([row, tombstone])
        if other_row_names_it:
            db_session.add(other_row)
        await db_session.commit()
        row_id, other_row_id, other_id, tombstone_id = row.id, other_row.id, other.id, tombstone.id
        context = await db_session.get(Context, ctx_id)
        try:
            indexer, _ = _db_indexer(db_session)
            event = _make_event()
            event.resource_id, event.version, event.op = slug, 1, "delete"

            await indexer._apply_delete(event, context, "kagura_memories")
            await db_session.commit()
            points = AsyncMock()
            with patch("services.resource_indexer.delete_points_from_qdrant", points):
                await indexer._drop_retired_points()

            assert await db_session.get(Memory, row_id) is None
            assert await db_session.get(Memory, tombstone_id) is None
            if other_row_names_it:
                points.assert_not_awaited()
                assert await db_session.get(Memory, other_row_id) is not None
            else:
                points.assert_awaited_once_with([str(point)], "kagura_memories")
        finally:
            await db_session.rollback()
            await db_session.execute(delete(Memory).where(Memory.context_id == other_id))
            await db_session.execute(delete(Context).where(Context.id == other_id))
            await db_session.commit()
            await _drop_indexed_resource(db_session, ws_id, ctx_id, resource_pk)


class TestTryExcludePointWriters:
    """#2248, against the real DB: the old-version drain never waits for a
    point writer, so it never queues the other writers behind it."""

    @pytest.mark.asyncio
    async def test_it_refuses_while_a_writer_is_open_and_takes_a_free_lock(self, db_session):
        from db.base import isolated_session
        from db.point_writer_lock import hold_point_writer_lock, try_exclude_point_writers

        await db_session.rollback()
        async with isolated_session(db_session) as writer:
            await hold_point_writer_lock(writer)
            async with isolated_session(db_session) as drain:
                assert await try_exclude_point_writers(drain) is False
                await drain.rollback()
            await writer.rollback()
        async with isolated_session(db_session) as drain:
            assert await try_exclude_point_writers(drain) is True
            await drain.rollback()


class TestRetiredPointDrainReleasesTheLockAgainstTheDb:
    """#2478, against the real DB: a drain stalled in the vector store holds
    the point writers out only until its bound; a writer that queued behind
    it then gets the lock."""

    @pytest.mark.asyncio
    async def test_a_writer_queued_behind_a_stalled_drain_gets_the_lock(self, db_session):
        from db.base import isolated_session
        from db.point_writer_lock import hold_point_writer_lock, try_exclude_point_writers
        from services.resource_indexer import RetiredVersionRow

        bound = 2.0
        await db_session.rollback()
        indexer, _ = _db_indexer(db_session)
        indexer._retired = [RetiredVersionRow(uuid4(), uuid4(), "kagura_memories")]
        stalled = asyncio.Event()

        async def _stall(_ids, _collection):
            stalled.set()
            await asyncio.Event().wait()

        # The probe and the writer connect before the drain starts, so once it
        # stalls each needs one round trip on an open connection, far inside
        # the bound, rather than a new connection racing it.
        async with (
            isolated_session(db_session) as probe,
            isolated_session(db_session) as writer,
        ):
            await probe.execute(select(1))
            await writer.execute(select(1))
            with (
                patch("services.resource_indexer.delete_points_from_qdrant", _stall),
                patch("services.resource_indexer.RETIRED_POINT_DELETE_TIMEOUT_SECONDS", bound),
            ):
                started = time.monotonic()
                drain = asyncio.create_task(indexer._drop_retired_points())
                try:
                    async with asyncio.timeout(bound + 10):
                        await stalled.wait()
                    held_during_stall = not await try_exclude_point_writers(probe)
                    await probe.rollback()
                    async with asyncio.timeout(bound + 10):
                        await hold_point_writer_lock(writer)
                    writer_waited = time.monotonic() - started
                    await writer.rollback()
                    async with asyncio.timeout(bound + 10):
                        await drain
                finally:
                    drain.cancel()

        assert held_during_stall
        # The drain's bound starts after ``started``, so a writer let in by
        # the bound waited at least that long; earlier means the lock went
        # before the bound, much later that the bound did not release it.
        assert bound - 0.05 <= writer_waited < bound + 5
        async with isolated_session(db_session) as after:
            assert await try_exclude_point_writers(after) is True
            await after.rollback()
