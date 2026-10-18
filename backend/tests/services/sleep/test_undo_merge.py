"""#1209: per-merge undo service.

Pins the correction-loop contract: one dedup merge is individually
reversible (row restore + vector re-embed + audited ``undo_merge`` action on
the same report), with a stable error code for every non-restorable state —
including the retention-purged case, whose message names the setting that
bounds reversibility.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.sleep.undo import UndoMergeError, undo_merge_action


def _action(*, action_type: str = "merge", phase: str = "dedup_merge") -> MagicMock:
    action = MagicMock()
    action.id = 42
    action.action_type = action_type
    action.phase = phase
    action.memory_id = uuid4()  # winner
    action.target_id = uuid4()  # loser
    action.report_id = uuid4()
    return action


def _report() -> MagicMock:
    report = MagicMock()
    report.user_id = "admin-user"
    report.workspace_id = uuid4()
    report.context_id = None  # default embedding info path (no config row)
    return report


@pytest.fixture(autouse=True)
def point_writer_lock():
    """#1798: the restore takes the point-writer lock — one ``db.execute`` more
    than the mocked sessions below script."""
    with patch("services.sleep.undo.hold_point_writer_lock", new=AsyncMock()) as lock:
        yield lock


def _db(first_row, memory=None) -> AsyncMock:
    """AsyncMock db: 1st execute -> (action, report) join, 2nd -> memory,
    3rd -> the restore UPDATE."""
    db = AsyncMock()
    join_result = MagicMock()
    join_result.first.return_value = first_row
    mem_result = MagicMock()
    mem_result.scalar_one_or_none.return_value = memory
    db.execute.side_effect = [join_result, mem_result, MagicMock()]
    return db


@pytest.mark.asyncio
async def test_action_not_found() -> None:
    db = _db(None)
    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")
    assert exc.value.code == "action_not_found"


@pytest.mark.asyncio
async def test_not_a_merge() -> None:
    db = _db((_action(action_type="promote"), _report()))
    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")
    assert exc.value.code == "not_a_merge"


@pytest.mark.asyncio
async def test_purged_loser_names_the_retention_setting() -> None:
    """Reversibility is bounded by the declared window — the error must say
    which setting bounded it."""
    db = _db((_action(), _report()), memory=None)
    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")
    assert exc.value.code == "memory_purged"
    assert "sleep_merge_retention_days" in exc.value.message
    # #1521: the platform cleanup sweep purges too, so the message must name
    # both purgers — an operator reading "0 = no additional Sleep purge" is
    # otherwise sent to the wrong setting.
    assert "CLEANUP_DELETED_MEMORIES_RETENTION_DAYS" in exc.value.message


@pytest.mark.asyncio
async def test_already_restored() -> None:
    loser = MagicMock()
    loser.deleted_at = None
    db = _db((_action(), _report()), memory=loser)
    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")
    assert exc.value.code == "already_restored"


@pytest.mark.asyncio
async def test_refuses_non_sleep_deletion() -> None:
    loser = MagicMock()
    loser.deleted_at = MagicMock()
    loser.deleted_by = "user_delete"
    db = _db((_action(), _report()), memory=loser)
    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")
    assert exc.value.code == "not_merge_deleted"


@pytest.mark.asyncio
async def test_restore_holds_the_point_writer_lock_before_the_vector_is_rebuilt(
    point_writer_lock,
) -> None:
    """#1798: until the restore commits, the row still reads as a tombstone to
    the orphan sweep, which would delete the rebuilt vector."""
    action = _action()
    loser = MagicMock()
    loser.id = action.target_id
    loser.deleted_at = MagicMock()
    loser.deleted_by = "sleep_maintenance"
    db = _db((action, _report()), memory=loser)
    order: list[str] = []
    point_writer_lock.side_effect = lambda _db: order.append("lock")

    async def re_embed(*_args, **_kwargs):
        order.append("re_embed")

    with (
        patch("services.sleep.undo.re_embed_memory_to_qdrant", new=re_embed),
        patch("services.embedding_service.EmbeddingService"),
    ):
        await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert order == ["lock", "re_embed"]


@pytest.mark.asyncio
async def test_happy_path_restores_reembeds_and_audits() -> None:
    action = _action()
    report = _report()
    loser = MagicMock()
    loser.id = action.target_id
    loser.deleted_at = MagicMock()
    loser.deleted_by = "sleep_maintenance"
    db = _db((action, report), memory=loser)

    with (
        patch("services.sleep.undo.re_embed_memory_to_qdrant", new=AsyncMock()) as re_embed,
        patch("services.embedding_service.EmbeddingService"),
    ):
        summary = await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert summary["restored_memory_id"] == str(action.target_id)
    assert summary["winner_id"] == str(action.memory_id)
    assert summary["undone_action_id"] == 42

    re_embed.assert_awaited_once()
    # #2015: the session is passed so the loser's chunk points are rebuilt.
    assert re_embed.await_args.kwargs["db"] is db

    # The undo is audited as an undo_merge action on the SAME report.
    added = db.add.call_args.args[0]
    assert added.action_type == "undo_merge"
    assert added.report_id == action.report_id
    assert added.details["undone_action_id"] == 42
    assert added.details["undone_by"] == "admin-user"
    db.commit.assert_awaited_once()


# ------------------------------------------------------------ shadow merges


def _shadow_action(prior_edge=None) -> MagicMock:
    action = _action()
    action.details = {"mode": "shadow", "prior_edge": prior_edge}
    return action


def _shadow_db(
    first_row,
    revert_rowcount: int,
    current_edge_type: str | None = None,
) -> AsyncMock:
    """AsyncMock db: 1st execute -> (action, report) join, 2nd -> the edge
    revert (UPDATE restore or verified DELETE) with the given rowcount.

    #1450: when the revert matches no row, the helper reads the edge back to
    tell "already undone" from "a later writer got there first", so a third
    execute is served — ``current_edge_type`` is what that read finds (None =
    no edge at all).
    """
    db = AsyncMock()
    join_result = MagicMock()
    join_result.first.return_value = first_row
    revert_result = MagicMock()
    revert_result.rowcount = revert_rowcount
    current_result = MagicMock()
    current_result.scalar_one_or_none.return_value = current_edge_type
    db.execute.side_effect = [join_result, revert_result, current_result]
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
async def test_shadow_undo_deletes_created_edge_and_audits() -> None:
    """#1208: a shadow merge never deleted the loser — undo means removing
    the supersedes edge, audited as undo_merge with mode=shadow."""
    action = _shadow_action(prior_edge=None)
    db = _shadow_db((action, _report()), revert_rowcount=1)

    summary = await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert summary["restored_memory_id"] == str(action.target_id)
    assert summary["undone_action_id"] == 42
    added = db.add.call_args.args[0]
    assert added.action_type == "undo_merge"
    assert added.details["mode"] == "shadow"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_shadow_undo_restores_prior_edge_state() -> None:
    """When the shadow merge retyped a pre-existing edge, undo must issue an
    UPDATE restoring the snapshot, not a DELETE (the association survives)."""
    from sqlalchemy.sql.dml import Update

    prior = {
        "edge_type": "neural_association",
        "origin": "hebbian",
        "weight": 0.4,
        "confidence": 0.9,
        "edge_metadata": None,
    }
    action = _shadow_action(prior_edge=prior)
    db = _shadow_db((action, _report()), revert_rowcount=1)

    await undo_merge_action(db, 42, acting_user_id="admin-user")

    revert_stmt = db.execute.call_args_list[1].args[0]
    assert isinstance(revert_stmt, Update)


@pytest.mark.asyncio
async def test_shadow_undo_without_snapshot_deletes() -> None:
    """No snapshot means the merge CREATED the edge — undo deletes it."""
    from sqlalchemy.sql.dml import Delete

    action = _shadow_action(prior_edge=None)
    db = _shadow_db((action, _report()), revert_rowcount=1)

    await undo_merge_action(db, 42, acting_user_id="admin-user")

    revert_stmt = db.execute.call_args_list[1].args[0]
    assert isinstance(revert_stmt, Delete)


@pytest.mark.asyncio
async def test_shadow_undo_gone_edge_is_already_restored() -> None:
    """Edge already gone → benign no-op, stable error, nothing audited."""
    action = _shadow_action(prior_edge=None)
    db = _shadow_db((action, _report()), revert_rowcount=0, current_edge_type=None)

    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert exc.value.code == "already_restored"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_undo_retyped_edge_is_not_already_restored() -> None:
    """#1450: a later writer's edge is NOT "already undone".

    Both matched zero rows and so shared one ``already_restored`` code, but the
    outcomes differ: nothing to do vs. the undo did not happen and someone has
    to look at the newer edge. Same 409, distinguishable ``error_code``.
    """
    action = _shadow_action(prior_edge=None)
    db = _shadow_db(
        (action, _report()),
        revert_rowcount=0,
        current_edge_type="neural_association",
    )

    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert exc.value.code == "edge_changed"
    assert "not reversed" in exc.value.message
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_undo_snapshot_already_restored_is_benign() -> None:
    """#1450: with a snapshot, "already undone" means the edge is back at the
    snapshot's type — that must not read as a later writer's interference."""
    prior = {"edge_type": "neural_association", "origin": "hebbian"}
    action = _shadow_action(prior_edge=prior)
    db = _shadow_db(
        (action, _report()),
        revert_rowcount=0,
        current_edge_type="neural_association",
    )

    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert exc.value.code == "already_restored"


@pytest.mark.asyncio
async def test_shadow_undo_snapshot_overwritten_is_blocked() -> None:
    """#1450: with a snapshot, an edge of some OTHER type means the snapshot
    can no longer be restored from here — blocked, not already-undone."""
    prior = {"edge_type": "neural_association", "origin": "hebbian"}
    action = _shadow_action(prior_edge=prior)
    db = _shadow_db(
        (action, _report()),
        revert_rowcount=0,
        current_edge_type="contradicts",
    )

    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert exc.value.code == "edge_changed"


@pytest.mark.asyncio
async def test_shadow_undo_snapshot_edge_deleted_is_blocked_without_claiming_shadow() -> None:
    """#1450 (review): with a snapshot, a DELETED edge is also un-restorable.

    Still blocked — the pre-merge weight/origin/metadata cannot be put back —
    but the message must not claim a live shadow edge is in the way. Reaching
    ANY blocked state means the guarded statement found no ``supersedes`` row,
    so nothing is shadowing the loser; what is lost is the edge state the merge
    overwrote. The first wording of this error said "the merge is still in
    effect", which is false in every blocked case.
    """
    prior = {"edge_type": "neural_association", "origin": "hebbian"}
    action = _shadow_action(prior_edge=prior)
    db = _shadow_db((action, _report()), revert_rowcount=0, current_edge_type=None)

    with pytest.raises(UndoMergeError) as exc:
        await undo_merge_action(db, 42, acting_user_id="admin-user")

    assert exc.value.code == "edge_changed"
    assert "changed or removed" in exc.value.message
    assert "still in effect" not in exc.value.message


@pytest.mark.asyncio
async def test_re_embed_rebuilds_content_chunks_when_given_a_session() -> None:
    """#2015: the restore helper rewrites the chunk points after the summary point."""
    from services.sleep.undo import re_embed_memory_to_qdrant

    memory = _restorable_memory()
    svc = MagicMock()
    svc.embed = AsyncMock(return_value=[0.1])
    db = _session()
    with (
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()),
        patch("services.content_chunk_writer.write_content_chunks", new=AsyncMock()) as write,
    ):
        await re_embed_memory_to_qdrant(memory, "u", svc, "w", "c", "coll", db=db)
        write.assert_awaited_once()
        assert write.await_args.args[:2] == (db, memory)
        assert write.await_args.kwargs["collection_name"] == "coll"

        write.reset_mock()
        await re_embed_memory_to_qdrant(memory, "u", svc, "w", "c", "coll")
        write.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_chunk_write_re_queues_the_restored_memory() -> None:
    """#2061: the merge deleted the loser's chunks with its vector; a chunk write
    that fails during the restore re-queues the row (same uncommitted restore
    transaction) instead of aborting the undo or leaving it summary-only."""
    from services.content_chunk_writer import ContentChunkWriteError
    from services.sleep.undo import re_embed_memory_to_qdrant

    memory = _restorable_memory()
    svc = MagicMock()
    svc.embed = AsyncMock(return_value=[0.1])
    db = _session()
    db.commit = AsyncMock()
    with (
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()) as upsert,
        patch(
            "services.content_chunk_writer.write_content_chunks",
            new=AsyncMock(side_effect=ContentChunkWriteError("Content chunk write failed (x)")),
        ) as write,
        patch(
            "services.content_chunk_writer.requeue_embedding", new=AsyncMock(return_value=True)
        ) as requeue,
    ):
        requeued = await re_embed_memory_to_qdrant(memory, "u", svc, "w", "c", "coll", db=db)

    assert requeued == "chunks"
    upsert.assert_awaited_once()  # the summary point is back
    assert write.await_args.kwargs["fail_open"] is False
    requeue.assert_awaited_once_with(db, memory.id, reason="chunk_write_failed")
    db.commit.assert_not_awaited()  # the caller commits the restore with it


@pytest.mark.asyncio
async def test_a_failed_re_queue_does_not_abort_the_restore() -> None:
    import structlog

    from services.sleep.undo import re_embed_memory_to_qdrant

    memory = _restorable_memory()
    svc = MagicMock()
    svc.embed = AsyncMock(return_value=[0.1])
    with (
        structlog.testing.capture_logs() as logs,
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()),
        patch(
            "services.content_chunk_writer.write_content_chunks",
            new=AsyncMock(side_effect=RuntimeError("x")),
        ),
        patch(
            "services.content_chunk_writer.requeue_embedding",
            new=AsyncMock(side_effect=RuntimeError("secret")),
        ),
    ):
        requeued = await re_embed_memory_to_qdrant(
            memory, "u", svc, "w", "c", "coll", db=_session()
        )

    assert requeued is None
    event = next(e for e in logs if e["event"] == "embedding_requeue_failed")
    assert event["error_class"] == "RuntimeError"
    assert "secret" not in repr(event)


@pytest.mark.asyncio
async def test_a_written_or_skipped_chunk_write_re_queues_nothing() -> None:
    from services.sleep.undo import re_embed_memory_to_qdrant

    svc = MagicMock()
    svc.embed = AsyncMock(return_value=[0.1])
    with (
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()),
        patch("services.content_chunk_writer.write_content_chunks", new=AsyncMock(return_value=3)),
        patch("services.content_chunk_writer.requeue_embedding", new=AsyncMock()) as requeue,
    ):
        assert (
            await re_embed_memory_to_qdrant(
                _restorable_memory(), "u", svc, "w", "c", "coll", db=_session()
            )
            is None
        )
    requeue.assert_not_awaited()


def _session() -> MagicMock:
    """A session double for the restore helper: it re-reads the row first (#2065)."""
    db = MagicMock()
    db.refresh = AsyncMock()
    return db


def _restorable_memory(**overrides: object) -> MagicMock:
    """A merge loser with the real column values build_memory_point reads."""
    from datetime import datetime

    memory = MagicMock()
    memory.id = uuid4()
    memory.user_id = "u"
    memory.summary = "s"
    memory.context_summary = "why"
    memory.content = "restored content body"
    memory.type = "note"
    memory.importance = 0.6
    memory.scope = "persistent"
    memory.tags = ["t"]
    memory.client = "claude-code"
    memory.context = None
    memory.created_at = datetime(2026, 1, 1)
    memory.updated_at = datetime(2026, 1, 2)
    memory.location_lat = None
    memory.location_lon = None
    memory.resource_id = None
    for key, value in overrides.items():
        setattr(memory, key, value)
    return memory


@pytest.mark.asyncio
async def test_re_embed_upsert_carries_bm25_sparse_and_canonical_payload() -> None:
    """#2053: an undo restores the same point the normal write path builds."""
    from services.memory_service import build_memory_point
    from services.sleep.undo import re_embed_memory_to_qdrant

    memory = _restorable_memory(
        summary="Undo restores the BM25 sparse vector",
        context={"issue": 2053},
        location_lat=35.68,
        location_lon=139.76,
    )
    svc = MagicMock()
    svc.embed = AsyncMock(return_value=[0.1])
    with (
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()) as upsert,
        patch("services.content_chunk_writer.write_content_chunks", new=AsyncMock()) as write,
    ):
        await re_embed_memory_to_qdrant(memory, "u", svc, "w", "c", "coll", db=_session())

    # Dense embedding is unchanged: the raw summary.
    assert svc.embed.await_args.args[0] == "Undo restores the BM25 sparse vector"
    kwargs = upsert.await_args.kwargs
    expected_payload, expected_idx, expected_vals = build_memory_point(memory)
    assert kwargs["sparse_indices"]
    assert kwargs["sparse_indices"] == expected_idx
    assert kwargs["sparse_values"] == expected_vals
    assert set(kwargs["payload"]) == set(expected_payload)
    assert kwargs["payload"]["location"] == {"lat": 35.68, "lon": 139.76}
    assert kwargs["payload"]["context"] == {"issue": 2053}
    assert write.await_args.kwargs["base_payload"] is kwargs["payload"]


def _resource_loser() -> MagicMock:
    """#2053: a merge loser the resource indexer wrote; its point is ``point_id``."""
    from services.resource_indexer import resource_point_id

    point_id = resource_point_id("reports", "doc-1", 2)
    return _restorable_memory(
        summary="[resource] doc-1 v2",
        content='{"title": "Annual report"}',
        resource_id="reports",
        resource_doc_id="doc-1",
        resource_version=2,
        summary_embedding_id=point_id,
        point_id=point_id,
    )


@pytest.mark.asyncio
async def test_re_embed_rebuilds_the_indexer_point_of_a_resource_row() -> None:
    """#2053: undo restores the document's point, not a label point under the row id."""
    from services.sleep.undo import re_embed_memory_to_qdrant

    memory = _resource_loser()
    svc = MagicMock()
    svc.embed = AsyncMock(return_value=[0.1])
    db = _session()
    indexer = MagicMock()
    indexer.rebuild_point = AsyncMock(return_value=memory.point_id)
    with (
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()) as upsert,
        patch("services.content_chunk_writer.write_content_chunks", new=AsyncMock()) as write,
        patch("services.resource_indexer.ResourceIndexer", return_value=indexer) as indexer_cls,
    ):
        await re_embed_memory_to_qdrant(memory, "u", svc, "w", "c", "coll", db=db)

    indexer_cls.assert_called_once_with(db)
    indexer.rebuild_point.assert_awaited_once_with(
        memory, collection_name="coll", embedding_service=svc, usage=None
    )
    upsert.assert_not_awaited()
    write.assert_not_awaited()
    svc.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_re_embed_of_a_resource_row_needs_a_session() -> None:
    from services.sleep.undo import re_embed_memory_to_qdrant

    svc = MagicMock()
    svc.embed = AsyncMock(return_value=[0.1])
    with (
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()) as upsert,
        pytest.raises(ValueError, match="needs a database session"),
    ):
        await re_embed_memory_to_qdrant(_resource_loser(), "u", svc, "w", "c", "coll")
    upsert.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_row", [False, True])
async def test_a_failed_point_write_re_queues_the_restored_memory(resource_row: bool) -> None:
    """#2065: a point that cannot be written during a restore (a resource whose
    schema is gone, a provider down) re-queues the row ``pending`` in the
    still-uncommitted restore transaction: the embedding sweep rebuilds it —
    through the indexer for a resource row — and the restore stands."""
    from services.resource_indexer import ResourceRebuildError
    from services.sleep.undo import re_embed_memory_to_qdrant

    memory = _resource_loser() if resource_row else _restorable_memory()
    svc = MagicMock()
    svc.embed = AsyncMock(side_effect=RuntimeError("provider down"))
    indexer = MagicMock()
    indexer.rebuild_point = AsyncMock(side_effect=ResourceRebuildError("schema gone"))
    db = _session()
    db.commit = AsyncMock()
    with (
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()) as upsert,
        patch("services.resource_indexer.ResourceIndexer", return_value=indexer),
        patch("services.content_chunk_writer.write_content_chunks", new=AsyncMock()) as write,
        patch("services.sleep.undo._requeue_restored", new=AsyncMock(return_value=True)) as requeue,
    ):
        requeued = await re_embed_memory_to_qdrant(memory, "u", svc, "w", "c", "coll", db=db)

    assert requeued == "point"
    requeue.assert_awaited_once_with(db, memory.id)
    db.refresh.assert_awaited_once_with(memory)  # re-read after the restore UPDATE
    upsert.assert_not_awaited()
    write.assert_not_awaited()
    db.commit.assert_not_awaited()  # the caller commits the restore with it


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requeue",
    [AsyncMock(return_value=False), AsyncMock(side_effect=RuntimeError("db down"))],
    ids=["row_gone", "requeue_failed"],
)
async def test_a_failed_point_write_that_cannot_be_re_queued_is_raised(requeue) -> None:
    """#2065: a row that cannot be re-queued must not be restored with no
    point: the write's own error is raised so the caller's restore does not
    commit."""
    from services.sleep.undo import re_embed_memory_to_qdrant

    svc = MagicMock()
    svc.embed = AsyncMock(side_effect=RuntimeError("provider down"))
    with (
        patch("services.memory_service.add_memory_to_qdrant", new=AsyncMock()),
        patch("services.sleep.undo._requeue_restored", new=requeue),
        pytest.raises(RuntimeError, match="provider down"),
    ):
        await re_embed_memory_to_qdrant(
            _restorable_memory(), "u", svc, "w", "c", "coll", db=_session()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("rowcount", [1, 0])
async def test_the_restored_row_is_re_queued_whatever_its_status(rowcount: int) -> None:
    """#2065: unlike ``requeue_embedding`` the UPDATE is not fenced on
    ``success``: the merge deleted the point, so a ``processing`` claim taken
    before it, or a ``failed`` row, must go back to the sweep too. It runs in
    a SAVEPOINT and matches only a live row."""
    from services.sleep.undo import _requeue_restored

    db = MagicMock()
    db.begin_nested = MagicMock(return_value=AsyncMock())
    db.execute = AsyncMock(return_value=MagicMock(rowcount=rowcount))
    assert await _requeue_restored(db, uuid4()) is bool(rowcount)
    db.begin_nested.assert_called_once()
    sql = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "embedding_status='pending'" in sql
    assert "memories.deleted_at IS NULL" in sql
    assert "embedding_status =" not in sql.split("WHERE", 1)[1]


@pytest.mark.asyncio
async def test_a_failed_point_write_without_a_session_is_raised() -> None:
    from services.sleep.undo import re_embed_memory_to_qdrant

    svc = MagicMock()
    svc.embed = AsyncMock(side_effect=RuntimeError("provider down"))
    with pytest.raises(RuntimeError, match="provider down"):
        await re_embed_memory_to_qdrant(_restorable_memory(), "u", svc, "w", "c", "coll")
