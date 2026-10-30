"""#2100: a metadata edit reaches the memory's own point, not its row id.

``update_memory`` and PATCH merge a metadata-only edit into the vector payload.
A resource-indexer row's point is ``Memory.point_id`` (the document version's
uuid5), so the merge goes there, by the rule ``forget`` and the Sleep paths use
(#2065, ``unshared_point_id``). A point another live row names too is left
alone. The merge writes only the keys a metadata edit changes; the indexer's
own payload fields — its event-time ``updated_at`` among them — are never sent,
nor is importance, which the Sleep paths keep off the indexer point (#2065).
Session rollback restores a resource row's payload by the same rule. The
vector store and the session are faked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from models.memory import Memory
from models.schemas import PatchMemoryRequest, UpdateMemoryRequest
from services.memory_service import MemoryService
from services.resource_indexer import resource_point_id

_RESOURCE_POINT = resource_point_id("reports", "doc-1", 2)

# The payload keys the indexer writes for itself (build_resource_point): a
# metadata edit must never send any of them.
_INDEXER_KEYS = {
    "workspace_id",
    "context_id",
    "user_id",
    "resource_id",
    "doc_id",
    "version",
    "content",
    "facets",
    "sortable",
    "metadata",
    "updated_at",
    "indexed_at",
    "memory_id",
    "point_id_source",
}


def _row(**overrides) -> Memory:
    fields = {
        "id": uuid4(),
        "workspace_id": uuid4(),
        "context_id": uuid4(),
        "user_id": "point-sync",
        "summary": "a memory summary",
        "content": "c",
        "type": "note",
        "importance": 0.5,
        "scope": "working",
        "client": "pytest",
    }
    fields.update(overrides)
    return Memory(**fields)


def _api_row() -> Memory:
    """A memory the API wrote: its point is its own id."""
    row = _row()
    row.summary_embedding_id = row.id
    return row


def _indexer_row() -> Memory:
    """A row the resource indexer wrote: it names its document's uuid5 point."""
    return _row(
        summary="[reports] doc-1 v2",
        type="resource_data",
        details={"resource_id": "reports", "doc_id": "doc-1", "version": 2},
        summary_embedding_id=_RESOURCE_POINT,
    )


def _db(*, shared: bool) -> MagicMock:
    """A session whose "does another live row name this point?" answer is ``shared``."""
    db = MagicMock()
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=nested)
    result = MagicMock()
    result.scalar.return_value = shared
    db.execute = AsyncMock(return_value=result)
    return db


async def _update(memory: Memory, request: UpdateMemoryRequest, *, shared: bool = False):
    service = MemoryService(_db(shared=shared))
    with (
        patch(
            "services.memory_service.update_memory_payload_in_qdrant", new_callable=AsyncMock
        ) as write,
        patch(
            "services.memory_service.resolve_collection_name",
            new=AsyncMock(return_value="kagura_memories"),
        ),
        patch("services.memory_service.chunk_cascade_needed", new=AsyncMock(return_value=True)),
    ):
        effective = request.details if request.details is not None else memory.details
        await service._update_sync_qdrant_payload(memory, request, None, effective)
    return write, service


async def _patch(memory: Memory, request: PatchMemoryRequest, *, shared: bool = False):
    service = MemoryService(_db(shared=shared))
    with (
        patch(
            "services.memory_service.update_memory_payload_in_qdrant", new_callable=AsyncMock
        ) as write,
        patch(
            "services.memory_service.resolve_collection_name",
            new=AsyncMock(return_value="kagura_memories"),
        ),
        patch("services.memory_service.chunk_cascade_needed", new=AsyncMock(return_value=True)),
    ):
        await service._patch_sync_qdrant_payload(
            memory, request, request.model_fields_set, memory.details, "point-sync"
        )
    return write, service


class TestUpdateSyncsTheRowsOwnPoint:
    async def test_a_resource_rows_edit_reaches_its_documents_point(self):
        memory = _indexer_row()

        write, _ = await _update(memory, UpdateMemoryRequest(memory_id=memory.id, tags=["q3"]))

        write.assert_awaited_once()
        assert write.await_args.kwargs["memory_id"] == _RESOURCE_POINT
        # Resource points have no content chunks.
        assert write.await_args.kwargs["cascade_chunks"] is False

    async def test_an_api_rows_edit_still_reaches_its_own_id(self):
        memory = _api_row()

        write, service = await _update(
            memory, UpdateMemoryRequest(memory_id=memory.id, importance=0.9)
        )

        assert write.await_args.kwargs["memory_id"] == memory.id
        assert write.await_args.kwargs["cascade_chunks"] is True
        # Its own point gets the whole patch, the edit time included.
        assert set(write.await_args.kwargs["payload_updates"]) == {"importance", "updated_at"}
        # Its own point needs no "is it shared?" lookup.
        service.db.execute.assert_not_awaited()

    async def test_an_importance_only_edit_of_a_resource_row_writes_nothing(self):
        memory = _indexer_row()

        write, _ = await _update(memory, UpdateMemoryRequest(memory_id=memory.id, importance=0.9))

        write.assert_not_awaited()

    async def test_a_point_another_live_row_names_is_left_alone(self):
        memory = _indexer_row()

        write, _ = await _update(
            memory, UpdateMemoryRequest(memory_id=memory.id, tags=["q3"]), shared=True
        )

        write.assert_not_awaited()

    async def test_only_the_edited_metadata_keys_are_merged(self):
        memory = _indexer_row()
        request = UpdateMemoryRequest(
            memory_id=memory.id,
            tags=["q3"],
            importance=0.8,
            type="report",
            details={"location": {"lat": 35.0, "lon": 139.0}},
        )

        write, _ = await _update(memory, request)

        updates = write.await_args.kwargs["payload_updates"]
        # importance and updated_at stay the indexer's (RESOURCE_POINT_SKIPPED_KEYS).
        assert set(updates) == {"tags", "type", "location"}
        assert updates["tags"] == ["q3"]
        assert not set(updates) & _INDEXER_KEYS
        # The details carry no observed_at: that key is cleared (#2330).
        assert write.await_args.kwargs["delete_keys"] == ["observed_at"]


class TestPatchSyncsTheRowsOwnPoint:
    async def test_a_resource_rows_patch_reaches_its_documents_point(self):
        memory = _indexer_row()

        write, _ = await _patch(memory, PatchMemoryRequest(type="report"))

        write.assert_awaited_once()
        assert write.await_args.kwargs["memory_id"] == _RESOURCE_POINT
        assert write.await_args.kwargs["cascade_chunks"] is False

    async def test_an_api_rows_patch_still_reaches_its_own_id(self):
        memory = _api_row()

        write, _ = await _patch(memory, PatchMemoryRequest(tags=["a"]))

        assert write.await_args.kwargs["memory_id"] == memory.id
        assert write.await_args.kwargs["cascade_chunks"] is True

    async def test_a_point_another_live_row_names_is_left_alone(self):
        memory = _indexer_row()

        write, _ = await _patch(memory, PatchMemoryRequest(tags=["a"]), shared=True)

        write.assert_not_awaited()

    async def test_only_the_patched_metadata_keys_are_merged(self):
        memory = _indexer_row()
        memory.details = {"resource_id": "reports", "doc_id": "doc-1", "version": 2}
        request = PatchMemoryRequest(
            tags=["a"], importance=0.7, type="report", details=memory.details
        )

        write, _ = await _patch(memory, request)

        updates = write.await_args.kwargs["payload_updates"]
        assert set(updates) == {"tags", "type"}
        assert not set(updates) & _INDEXER_KEYS
        # The details carry no location and no observed_at (#2330): those
        # keys are cleared, nothing else.
        assert write.await_args.kwargs["delete_keys"] == ["location", "observed_at"]

    async def test_a_failed_shared_lookup_is_logged_not_raised(self):
        memory = _indexer_row()
        service = MemoryService(_db(shared=False))
        service.db.execute = AsyncMock(side_effect=RuntimeError("db down"))

        with patch(
            "services.memory_service.update_memory_payload_in_qdrant", new_callable=AsyncMock
        ) as write:
            await service._patch_sync_qdrant_payload(
                memory, PatchMemoryRequest(tags=["a"]), {"tags"}, None, "point-sync"
            )

        write.assert_not_awaited()


class TestSessionRollbackRestoresTheRowsOwnPoint:
    @staticmethod
    def _executor(db: MagicMock):
        from services import session_rollback as sr

        executor = sr._Executor.__new__(sr._Executor)
        executor.db = db
        executor.scope = MagicMock(context_id=uuid4())
        executor._payload_target = None
        executor._item_syncs = []
        executor._collection = AsyncMock(return_value="coll")  # type: ignore[method-assign]
        return executor

    async def _queue(self, memory: Memory, *, shared: bool = False) -> list:
        from utils.datetime import utcnow

        memory.updated_at = utcnow()
        executor = self._executor(_db(shared=shared))
        with patch(
            "services.content_chunk_writer.chunk_cascade_needed", new=AsyncMock(return_value=True)
        ):
            await executor._queue_payload_sync(memory, {"tags", "importance", "type"})
        return executor._item_syncs

    async def test_a_resource_rows_restore_targets_its_documents_point(self):
        memory = _indexer_row()
        memory.tags = ["orig"]

        (sync,) = await self._queue(memory)

        assert sync.memory_id == memory.id
        assert sync.target == _RESOURCE_POINT
        assert sync.cascade_chunks is False
        assert set(sync.updates) == {"tags", "type"}

    async def test_an_api_rows_restore_still_targets_its_own_id(self):
        memory = _api_row()
        memory.tags = ["orig"]

        (sync,) = await self._queue(memory)

        assert sync.target == memory.id
        assert sync.cascade_chunks is True
        assert set(sync.updates) == {"tags", "importance", "type", "updated_at"}

    async def test_a_shared_point_is_not_queued(self):
        memory = _indexer_row()
        memory.tags = ["orig"]

        assert await self._queue(memory, shared=True) == []

    def test_the_reread_patch_of_a_resource_point_drops_the_skipped_keys(self):
        from services import session_rollback as sr
        from utils.datetime import utcnow

        memory = _indexer_row()
        memory.tags = ["orig"]
        memory.updated_at = utcnow()
        sync = sr.PayloadSync(
            memory.id, "coll", ("importance", "tags"), {}, (), False, point_id=_RESOURCE_POINT
        )

        updates, delete_keys = sr._sync_patch(sync, memory)

        assert updates == {"tags": ["orig"]}
        assert delete_keys == []


class TestRollbackApplyChecksThePointAgain:
    @staticmethod
    async def _apply(row_point: object) -> AsyncMock:
        from types import SimpleNamespace

        from services import session_rollback as sr

        memory_id = uuid4()
        sync = sr.PayloadSync(
            memory_id, "coll", ("tags",), {"tags": ["a"]}, (), False, point_id=_RESOURCE_POINT
        )
        row = SimpleNamespace(
            id=memory_id,
            tags=["a"],
            importance=0.5,
            type="resource_data",
            details=None,
            updated_at=None,
            summary_embedding_id=row_point,
        )
        with (
            patch(
                "services.payload_sync.share_lock_rows",
                AsyncMock(return_value={memory_id: row}),
            ),
            patch("db.qdrant.update_memory_payload_in_qdrant", new_callable=AsyncMock) as write,
        ):
            await sr._apply_payload_syncs([sync], MagicMock(commit=AsyncMock()))
        return write

    async def test_the_resolved_point_is_patched(self):
        write = await self._apply(_RESOURCE_POINT)

        assert write.await_args.kwargs["memory_id"] == _RESOURCE_POINT
        assert write.await_args.kwargs["payload_updates"] == {"tags": ["a"]}

    async def test_a_point_moved_since_the_restore_is_left_alone(self):
        write = await self._apply(resource_point_id("reports", "doc-1", 3))

        write.assert_not_awaited()

    async def test_a_failed_reread_never_writes_the_queued_values(self):
        """#2346: the queued values may be older than an edit committed since."""
        from services import session_rollback as sr

        sync = sr.PayloadSync(uuid4(), "coll", ("tags",), {"tags": ["stale"]}, (), False)
        db = MagicMock(commit=AsyncMock(), rollback=AsyncMock())
        with (
            patch(
                "services.payload_sync.share_lock_rows",
                AsyncMock(side_effect=RuntimeError("read")),
            ),
            patch("db.qdrant.update_memory_payload_in_qdrant", new_callable=AsyncMock) as write,
        ):
            await sr._apply_payload_syncs([sync], db)

        write.assert_not_awaited()
        db.rollback.assert_awaited_once()


def test_scope_is_not_editable_by_update_or_patch():
    """``scope`` is not a metadata edit, so the sync never writes it."""
    assert "scope" not in UpdateMemoryRequest.model_fields
    assert "scope" not in PatchMemoryRequest.model_fields
