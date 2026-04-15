"""Unit tests for ResourceIndexer Qdrant upsert contract.

Issue #324: kagura_memories collection uses named vectors
({"dense": ..., "bm25": ...}); indexer must upsert with dict-keyed
vector, not anonymous.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from db.qdrant import KAGURA_MEMORIES_VECTOR_NAME
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

        await indexer._apply_upsert(event, schema, context)

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
    async def test_apply_upsert_point_id_is_deterministic_uuid(self, indexer):
        """uuid5 of resource_id:doc_id:v{version} must produce a stable point_id
        (idempotency for re-queue after Issue #324 backfill)."""
        event = _make_event()
        schema = _make_schema()
        context = _make_context()

        await indexer._apply_upsert(event, schema, context)
        first_id = indexer.qdrant_client.upsert.await_args.kwargs["points"][0].id

        indexer.qdrant_client.upsert.reset_mock()

        await indexer._apply_upsert(event, schema, context)
        second_id = indexer.qdrant_client.upsert.await_args.kwargs["points"][0].id

        assert first_id == second_id
