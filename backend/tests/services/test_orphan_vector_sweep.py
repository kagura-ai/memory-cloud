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
from sqlalchemy import delete

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


def _ref(
    point_id, *, context_id=None, is_resource=False, resource_key=None, written_at=None
) -> PointRef:
    return PointRef(
        point_id=str(point_id),
        context_id=str(context_id) if context_id else None,
        is_resource=is_resource,
        resource_key=resource_key,
        written_at=written_at,
    )


# A resource point old enough for the no-row rule (the sweep's default grace is 1 h).
OLD_ENOUGH = utcnow() - timedelta(days=1)


async def _live_context(db_session) -> Context:
    workspace = Workspace(id=uuid4(), name="Sweep", owner_user_id="sweep_user")
    db_session.add(workspace)
    await db_session.flush()
    context = Context(
        id=uuid4(),
        workspace_id=workspace.id,
        name=f"sweep_{uuid4().hex[:8]}",
        display_name="Sweep",
        created_by="sweep_user",
    )
    db_session.add(context)
    await db_session.flush()
    return context


def _resource_memory(context: Context, point_id, *, doc_id="doc_1", version=1, **overrides):
    """A resource-ingested row: the point id lives in summary_embedding_id and
    the document's natural key in details (the generated columns read it)."""
    return _memory(
        workspace_id=context.workspace_id,
        context_id=context.id,
        summary_embedding_id=point_id,
        details={"resource_id": "res_1", "doc_id": doc_id, "version": str(version)},
        **overrides,
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

    # --- #1829: a resource point in a live context follows its own row -------

    @pytest.mark.asyncio
    async def test_resource_point_with_a_live_row_is_kept(self, db_session):
        context = await _live_context(db_session)
        point_id = uuid4()
        db_session.add(_resource_memory(context, point_id))
        await db_session.flush()
        store = _FakeStore(
            [
                _ref(
                    point_id,
                    context_id=context.id,
                    is_resource=True,
                    resource_key=("res_1", "doc_1", 1),
                )
            ]
        )

        result = await _sweep(db_session, store, dry_run=False)

        assert result.orphans == 0
        assert store.deleted == []

    @pytest.mark.asyncio
    async def test_forgotten_resource_memory_loses_its_point_after_the_grace_period(
        self, db_session
    ):
        """forget soft-deletes the row; the point it could not delete by row id
        (#1829) goes with the next sweep — but not before the grace period."""
        context = await _live_context(db_session)
        fresh_id, old_id = uuid4(), uuid4()
        db_session.add_all(
            [
                _resource_memory(
                    context, fresh_id, doc_id="fresh", deleted_at=utcnow() - timedelta(minutes=5)
                ),
                _resource_memory(
                    context, old_id, doc_id="old", deleted_at=utcnow() - timedelta(hours=2)
                ),
            ]
        )
        await db_session.flush()
        store = _FakeStore(
            [
                _ref(fresh_id, context_id=context.id, is_resource=True),
                _ref(old_id, context_id=context.id, is_resource=True),
            ]
        )

        result = await _sweep(db_session, store, dry_run=False, grace=timedelta(hours=1))

        assert store.deleted == [str(old_id)]
        assert result.collections[0].resource_tombstoned == 1

    @pytest.mark.asyncio
    async def test_a_live_row_keeps_the_point_even_beside_an_old_tombstone(self, db_session):
        """forget, then sync again: the new row gets the same uuid5 point id the
        tombstone still names. The live row wins, whatever order the rows come in."""
        context = await _live_context(db_session)
        point_id = uuid4()
        db_session.add_all(
            [
                _resource_memory(context, point_id, deleted_at=utcnow() - timedelta(days=2)),
                _resource_memory(context, point_id),
            ]
        )
        await db_session.flush()
        store = _FakeStore([_ref(point_id, context_id=context.id, is_resource=True)])

        result = await _sweep(db_session, store, dry_run=False)

        assert result.orphans == 0
        assert store.deleted == []

    @pytest.mark.asyncio
    async def test_resource_point_whose_row_was_purged_is_judged_by_the_natural_key(
        self, db_session
    ):
        """No row by point id: a live row for the same document keeps the point
        (a re-index moved it), no such row makes it an orphan."""
        context = await _live_context(db_session)
        kept_id, gone_id = uuid4(), uuid4()
        # The document "doc_1" is live under some point id; the store still has
        # another point for it (kept), and one for a document no row names.
        db_session.add(_resource_memory(context, uuid4(), doc_id="doc_1"))
        await db_session.flush()
        store = _FakeStore(
            [
                _ref(
                    kept_id,
                    context_id=context.id,
                    is_resource=True,
                    resource_key=("res_1", "doc_1", 1),
                    written_at=OLD_ENOUGH,
                ),
                _ref(
                    gone_id,
                    context_id=context.id,
                    is_resource=True,
                    resource_key=("res_1", "doc_9", 1),
                    written_at=OLD_ENOUGH,
                ),
            ]
        )

        result = await _sweep(db_session, store, dry_run=False)

        assert store.deleted == [str(gone_id)]
        assert result.collections[0].resource_no_row == 1

    @pytest.mark.asyncio
    async def test_a_fresh_point_with_no_row_is_kept(self, db_session):
        """The indexer writes the point before the transaction that owns the row
        commits: a point younger than the grace period is never a no-row orphan."""
        context = await _live_context(db_session)
        store = _FakeStore(
            [
                _ref(
                    uuid4(),
                    context_id=context.id,
                    is_resource=True,
                    resource_key=("res_1", "doc_new", 1),
                    written_at=utcnow() - timedelta(minutes=2),
                ),
                # No timestamp at all: undecidable, kept.
                _ref(
                    uuid4(),
                    context_id=context.id,
                    is_resource=True,
                    resource_key=("res_1", "doc_old", 1),
                ),
            ]
        )

        result = await _sweep(db_session, store, dry_run=False)

        assert result.orphans == 0
        assert store.deleted == []

    @pytest.mark.asyncio
    async def test_a_backlog_point_written_just_now_with_no_row_is_kept(self, db_session):
        """#1869: the indexer working through a backlog writes points whose
        payload ``updated_at`` (the event time) is long past the grace period
        while the batch that owns their rows has not committed. The sweep reads
        the real write time (``indexed_at``), so such a point is kept."""
        from types import SimpleNamespace

        from db.qdrant import _point_ref

        context = await _live_context(db_session)
        now = utcnow()
        point = SimpleNamespace(
            id=str(uuid4()),
            payload={
                "context_id": str(context.id),
                "resource_id": "res_1",
                "doc_id": "doc_backlog",
                "version": 1,
                "updated_at": (now - timedelta(days=3)).isoformat() + "Z",
                "indexed_at": (now - timedelta(minutes=2)).isoformat() + "Z",
            },
        )
        store = _FakeStore([_point_ref(point)])

        result = await _sweep(db_session, store, dry_run=False)

        assert result.orphans == 0
        assert store.deleted == []

    @pytest.mark.asyncio
    async def test_a_point_left_by_a_rolled_back_batch_is_swept_after_the_grace_period(
        self, db_session
    ):
        """#1869: a batch that rolled back leaves its points without rows for
        good; once the write time is past the grace period they are orphans."""
        from types import SimpleNamespace

        from db.qdrant import _point_ref

        context = await _live_context(db_session)
        now = utcnow()
        point_id = str(uuid4())
        point = SimpleNamespace(
            id=point_id,
            payload={
                "context_id": str(context.id),
                "resource_id": "res_1",
                "doc_id": "doc_rolled_back",
                "version": 1,
                "updated_at": (now - timedelta(days=3)).isoformat() + "Z",
                "indexed_at": (now - timedelta(hours=2)).isoformat() + "Z",
            },
        )
        store = _FakeStore([_point_ref(point)])

        result = await _sweep(db_session, store, dry_run=False)

        assert store.deleted == [point_id]
        assert result.collections[0].resource_no_row == 1

    @pytest.mark.asyncio
    async def test_resource_point_without_a_natural_key_and_no_row_is_kept(self, db_session):
        """Points written before the payload carried doc_id/version cannot be
        judged when no row names them: keep them."""
        context = await _live_context(db_session)
        store = _FakeStore([_ref(uuid4(), context_id=context.id, is_resource=True)])

        result = await _sweep(db_session, store, dry_run=False)

        assert result.orphans == 0
        assert store.deleted == []


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
        # Committed: the sweep ends its own read transactions (#1804), so rows
        # that were only flushed would not be there when it counts.
        before = (await _sweep(db_session, _FakeStore([]))).live_embedded_memories
        rows = [
            _memory(),
            _memory(embedding_status="pending"),
            _memory(deleted_at=utcnow()),
        ]
        ids = [m.id for m in rows]
        db_session.add_all(rows)
        await db_session.commit()
        try:
            after = (await _sweep(db_session, _FakeStore([]))).live_embedded_memories
        finally:
            await db_session.execute(delete(Memory).where(Memory.id.in_(ids)))
            await db_session.commit()

        assert after == before + 1


class TestReadTransactions:
    """#1804: no read transaction stays open across the scan."""

    @pytest.mark.asyncio
    async def test_no_transaction_is_open_while_the_next_page_is_fetched(self, db_session):
        seen: list[bool] = []

        async def scroll(collection_name, *, page_size=1000):
            for _ in range(3):
                seen.append(db_session.in_transaction())
                yield [_ref(uuid4())]
            seen.append(db_session.in_transaction())

        with patch.object(sweep_module, "scroll_point_refs", scroll):
            result = await sweep_orphan_points(
                db_session, dry_run=True, collections=["kagura_memories_a", "kagura_memories_b"]
            )

        assert result.scanned == 6
        assert seen and not any(seen)
        assert not db_session.in_transaction()

    @pytest.mark.asyncio
    async def test_a_failed_lookup_still_ends_the_transaction(self, db_session):
        async def scroll(collection_name, *, page_size=1000):
            yield [_ref(uuid4())]

        with (
            patch.object(sweep_module, "scroll_point_refs", scroll),
            patch.object(sweep_module, "_classify", AsyncMock(side_effect=RuntimeError("db"))),
            pytest.raises(RuntimeError),
        ):
            await sweep_orphan_points(db_session, dry_run=True, collections=[COLLECTION])

        assert not db_session.in_transaction()


class TestPartialFailures:
    @pytest.mark.asyncio
    async def test_a_scan_that_fails_part_way_reports_no_orphans_for_that_collection(
        self, db_session
    ):
        """Its candidates are dropped, so its counts must not be asked about
        or weighed against the ratio guard."""

        async def scroll(collection_name, *, page_size=1000):
            yield [_ref(uuid4()), _ref(uuid4())]
            raise QdrantError("scroll failed on page 2")

        with (
            patch.object(sweep_module, "scroll_point_refs", scroll),
            patch.object(sweep_module, "delete_points_from_qdrant", AsyncMock()) as delete,
        ):
            result = await sweep_orphan_points(
                db_session, dry_run=False, collections=[COLLECTION], max_orphan_ratio=0.5
            )

        assert result.collections[0].error is not None
        assert result.orphans == 0
        assert result.refused is None
        delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_delete_that_fails_does_not_stop_the_other_collections(self, db_session):
        first, second = uuid4(), uuid4()
        refs = {"kagura_memories_a": [_ref(first)], "kagura_memories_b": [_ref(second)]}
        deleted: list[str] = []

        async def scroll(collection_name, *, page_size=1000):
            yield refs[collection_name]

        async def delete(point_ids, collection_name):
            if collection_name == "kagura_memories_a":
                raise QdrantError("collection dropped")
            deleted.extend(point_ids)

        with (
            patch.object(sweep_module, "scroll_point_refs", scroll),
            patch.object(sweep_module, "delete_points_from_qdrant", delete),
        ):
            result = await sweep_orphan_points(db_session, dry_run=False, collections=list(refs))

        failed, swept = result.collections
        assert failed.error is not None
        assert failed.deleted == 0
        assert swept.deleted == 1
        assert deleted == [str(second)]


class TestLockIsHeldOneBatchAtATime:
    @pytest.mark.asyncio
    async def test_the_lock_is_retaken_for_every_batch(self, db_session):
        """Writers wait for one batch, never for the whole backlog."""
        store = _FakeStore([_ref(uuid4()), _ref(uuid4()), _ref(uuid4())])

        with (
            patch.object(sweep_module, "_DELETE_BATCH", 1),
            patch.object(
                sweep_module, "wait_for_point_writers", AsyncMock(return_value=True)
            ) as wait,
        ):
            result = await _sweep(db_session, store, dry_run=False)

        assert wait.await_count == 3
        assert result.deleted == 3

    @pytest.mark.asyncio
    async def test_a_writer_that_turns_up_mid_run_stops_it_and_says_how_far_it_got(
        self, db_session
    ):
        store = _FakeStore([_ref(uuid4()), _ref(uuid4())])

        with (
            patch.object(sweep_module, "_DELETE_BATCH", 1),
            patch.object(
                sweep_module, "wait_for_point_writers", AsyncMock(side_effect=[True, False])
            ),
        ):
            result = await _sweep(db_session, store, dry_run=False)

        assert result.deleted == 1
        assert result.refused is not None
        assert "deleting 1" in result.refused

    @pytest.mark.asyncio
    async def test_the_ratio_guard_weighs_only_the_collections_it_could_read(self, db_session):
        """A half-read collection adds neither orphans nor points to the share."""
        live = _memory()
        db_session.add(live)
        await db_session.flush()
        gone = [uuid4(), uuid4()]
        healthy = [_ref(live.id), *(_ref(g) for g in gone)]

        async def scroll(collection_name, *, page_size=1000):
            if collection_name == "kagura_memories_broken":
                yield [_ref(uuid4()) for _ in range(50)]
                raise QdrantError("scroll failed on page 2")
            yield healthy

        with (
            patch.object(sweep_module, "scroll_point_refs", scroll),
            patch.object(sweep_module, "delete_points_from_qdrant", AsyncMock()) as delete,
        ):
            result = await sweep_orphan_points(
                db_session,
                dry_run=False,
                collections=["kagura_memories_broken", COLLECTION],
                max_orphan_ratio=0.5,
            )

        # 2 of the 3 readable points are orphans; the 50 half-read ones do not
        # make that look like 2 of 53.
        assert result.refused is not None
        delete.assert_not_awaited()
