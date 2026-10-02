"""#1798: the orphan vector sweep — what it deletes, and what it never does.

The vector store is faked (the live round trip is in
``tests/integration/test_context_delete_vectors_qdrant.py``); the rows are
real, because the sweep's verdict is a Postgres lookup.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from db.qdrant import PointRef
from models.auth import Context, Workspace
from models.memory import Memory
from services import orphan_vector_sweep as sweep_module
from services.orphan_vector_sweep import sweep_orphan_points
from utils.datetime import utcnow
from utils.exceptions import QdrantError

COLLECTION = "kagura_memories"


def _memory(**overrides) -> Memory:
    fields = {
        "id": uuid4(),
        "user_id": "sweep_user",
        "summary": "a summary long enough",
        "content": "c",
        "type": "note",
        "client": "pytest",
        "embedding_status": "success",
    }
    fields.update(overrides)
    return Memory(**fields)


def _ref(point_id, *, context_id=None, is_resource=False) -> PointRef:
    return PointRef(
        point_id=str(point_id),
        context_id=str(context_id) if context_id else None,
        is_resource=is_resource,
    )


class _FakeStore:
    """Stands in for the vector store functions the sweep imports."""

    def __init__(self, refs: list[PointRef]):
        self.refs = refs
        self.deleted: list[str] = []

    async def scroll(self, collection_name, *, page_size=1000):
        yield list(self.refs)

    async def delete(self, point_ids, collection_name):
        self.deleted.extend(point_ids)

    def patched(self):
        return (
            patch.object(sweep_module, "scroll_point_refs", self.scroll),
            patch.object(sweep_module, "delete_points_from_qdrant", self.delete),
        )


async def _sweep(db_session, store: _FakeStore, **kwargs):
    kwargs.setdefault("collections", [COLLECTION])
    scroll, delete = store.patched()
    with scroll, delete:
        return await sweep_orphan_points(db_session, **kwargs)


class TestWhatCountsAsAnOrphan:
    @pytest.mark.asyncio
    async def test_live_memory_point_is_never_deleted(self, db_session):
        live = _memory()
        db_session.add(live)
        await db_session.flush()
        store = _FakeStore([_ref(live.id)])

        result = await _sweep(db_session, store, dry_run=False)

        assert result.orphans == 0
        assert store.deleted == []

    @pytest.mark.asyncio
    async def test_live_memory_in_a_deleted_context_is_kept(self, db_session):
        """The row decides for a memory point; a dead context does not overrule it."""
        workspace = Workspace(id=uuid4(), name="Sweep", owner_user_id="sweep_user")
        db_session.add(workspace)
        await db_session.flush()
        context = Context(
            id=uuid4(),
            workspace_id=workspace.id,
            name=f"sweep_{uuid4().hex[:8]}",
            display_name="Sweep",
            created_by="sweep_user",
            deleted_at=utcnow() - timedelta(days=3),
        )
        db_session.add(context)
        live = _memory(workspace_id=workspace.id, context_id=context.id)
        db_session.add(live)
        await db_session.flush()
        store = _FakeStore([_ref(live.id, context_id=context.id)])

        result = await _sweep(db_session, store, dry_run=False)

        assert result.orphans == 0
        assert store.deleted == []

    @pytest.mark.asyncio
    async def test_grace_period_spares_a_fresh_tombstone(self, db_session):
        fresh = _memory(deleted_at=utcnow() - timedelta(minutes=5))
        old = _memory(deleted_at=utcnow() - timedelta(hours=2))
        db_session.add_all([fresh, old])
        await db_session.flush()
        store = _FakeStore([_ref(fresh.id), _ref(old.id)])

        result = await _sweep(db_session, store, dry_run=False, grace=timedelta(hours=1))

        assert store.deleted == [str(old.id)]
        assert result.collections[0].tombstoned == 1

    @pytest.mark.asyncio
    async def test_undecidable_points_are_kept(self, db_session):
        """A non-UUID id and a resource point without a context cannot be judged."""
        store = _FakeStore([_ref("12345"), _ref(uuid4(), is_resource=True)])

        result = await _sweep(db_session, store, dry_run=False)

        assert result.scanned == 2
        assert result.orphans == 0
        assert store.deleted == []

    @pytest.mark.asyncio
    async def test_resource_point_follows_its_context(self, db_session):
        missing_context = uuid4()
        store = _FakeStore([_ref(uuid4(), context_id=missing_context, is_resource=True)])

        result = await _sweep(db_session, store, dry_run=False)

        assert result.collections[0].context_deleted == 1
        assert len(store.deleted) == 1


class TestSafetyRails:
    @pytest.mark.asyncio
    async def test_dry_run_deletes_nothing(self, db_session):
        store = _FakeStore([_ref(uuid4())])

        result = await _sweep(db_session, store, dry_run=True)

        assert result.orphans == 1
        assert result.collections[0].no_row == 1
        assert result.deleted == 0
        assert store.deleted == []

    @pytest.mark.asyncio
    async def test_row_that_lands_while_the_sweep_waits_saves_its_point(self, db_session):
        """merge_contexts upserts the point before its row is durable; the
        sweep waits for it on the point-writer lock and then looks again."""
        late = _memory()
        gone = uuid4()
        store = _FakeStore([_ref(late.id), _ref(gone)])

        async def merge_finishes(_db, *, timeout_seconds):
            db_session.add(late)
            await db_session.flush()
            return True

        with patch.object(
            sweep_module, "wait_for_point_writers", AsyncMock(side_effect=merge_finishes)
        ):
            result = await _sweep(db_session, store, dry_run=False)

        assert result.orphans == 2
        assert store.deleted == [str(gone)]
        assert result.deleted == 1

    @pytest.mark.asyncio
    async def test_dry_run_does_not_wait_for_point_writers(self, db_session):
        store = _FakeStore([_ref(uuid4())])

        with patch.object(sweep_module, "wait_for_point_writers", AsyncMock()) as wait:
            await _sweep(db_session, store, dry_run=True)

        wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_writer_that_does_not_finish_in_time_stops_the_delete(self, db_session):
        store = _FakeStore([_ref(uuid4())])

        with patch.object(sweep_module, "wait_for_point_writers", AsyncMock(return_value=False)):
            result = await _sweep(db_session, store, dry_run=False)

        assert result.refused is not None
        assert result.deleted == 0
        assert store.deleted == []

    @pytest.mark.asyncio
    async def test_unreadable_collection_is_skipped_not_fatal(self, db_session):
        """A collection dropped mid-run must not cost the others their sweep."""
        gone = uuid4()
        store = _FakeStore([_ref(gone)])

        async def scroll(collection_name, *, page_size=1000):
            if collection_name == "kagura_memories_dropped":
                raise QdrantError("Not found: Collection `kagura_memories_dropped` doesn't exist")
            yield list(store.refs)

        with (
            patch.object(sweep_module, "scroll_point_refs", scroll),
            patch.object(sweep_module, "delete_points_from_qdrant", store.delete),
        ):
            result = await sweep_orphan_points(
                db_session,
                dry_run=False,
                collections=["kagura_memories_dropped", COLLECTION],
            )

        dropped, swept = result.collections
        assert dropped.error is not None
        assert dropped.deleted == 0
        assert swept.deleted == 1
        assert store.deleted == [str(gone)]

    @pytest.mark.asyncio
    async def test_refuses_when_most_of_the_store_looks_orphaned(self, db_session):
        """What a sweep pointed at the wrong database looks like."""
        live = _memory()
        db_session.add(live)
        await db_session.flush()
        store = _FakeStore([_ref(live.id), _ref(uuid4()), _ref(uuid4())])

        result = await _sweep(db_session, store, dry_run=False, max_orphan_ratio=0.5)

        assert result.refused is not None
        assert result.deleted == 0
        assert store.deleted == []

    @pytest.mark.asyncio
    async def test_ratio_within_bounds_proceeds(self, db_session):
        live = [_memory() for _ in range(3)]
        db_session.add_all(live)
        await db_session.flush()
        gone = uuid4()
        store = _FakeStore([*(_ref(m.id) for m in live), _ref(gone)])

        result = await _sweep(db_session, store, dry_run=False, max_orphan_ratio=0.5)

        assert result.refused is None
        assert store.deleted == [str(gone)]
        assert result.remaining == 3

    @pytest.mark.asyncio
    async def test_reports_live_embedded_memories(self, db_session):
        before = (await _sweep(db_session, _FakeStore([]))).live_embedded_memories
        db_session.add_all(
            [
                _memory(),
                _memory(embedding_status="pending"),
                _memory(deleted_at=utcnow()),
            ]
        )
        await db_session.flush()

        after = (await _sweep(db_session, _FakeStore([]))).live_embedded_memories

        assert after == before + 1
