"""rollback_session's planner and executor without a database (#2044).

``plan_memory`` is a pure function of the session's events on one memory and
the memory's state; every row of the module's cause table is pinned here. The
executor tests pin the SAVEPOINT-per-item isolation and that a dry run never
locks, inserts or flushes. The SQL side runs on Postgres in
tests/integration/test_session_rollback_db.py.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import session_rollback as sr
from services.session_rollback import (
    ACTION_FORGET,
    ACTION_RESTORE,
    ACTION_SKIP,
    ACTION_UNFORGET,
    MemoryState,
    PlanItem,
    SessionEvent,
    combined_prior,
    plan_counts,
    plan_memory,
)

MID = uuid.uuid4()
ME = "me"


def ev(i: int, kind: str, version: int, prior: dict[str, Any] | None = None, actor: str = ME):
    return SessionEvent(
        id=i, memory_id=MID, kind=kind, version=version, prior=prior or {}, actor_user_id=actor
    )


LIVE = {"summary": "now", "tags": ["new"], "importance": 0.9}


def state(**kw: Any) -> MemoryState:
    return MemoryState(**{"exists": True, "current": LIVE, **kw})


@pytest.mark.parametrize(
    ("events", "st", "action", "cause"),
    [
        # created by the session
        ([ev(1, "created", 1)], state(version=1), ACTION_FORGET, None),
        (
            [ev(1, "created", 1), ev(2, "updated", 2, {"summary": "a"})],
            state(version=2),
            ACTION_FORGET,
            None,
        ),
        (
            [ev(1, "created", 1), ev(2, "forgotten", 1)],
            state(version=1, deleted=True, deleted_by=ME),
            ACTION_SKIP,
            "already_forgotten",
        ),
        (
            [ev(1, "created", 1)],
            state(version=1, deleted=True, deleted_by="sleep_maintenance"),
            ACTION_SKIP,
            "merged_by_sleep",
        ),
        (
            [ev(1, "created", 1)],
            state(version=1, deleted=True, deleted_by="sleep_consolidation"),
            ACTION_SKIP,
            "merged_by_sleep",
        ),
        # a version moved by someone else, or another writer's later event
        ([ev(1, "created", 1)], state(version=2), ACTION_SKIP, "modified_since"),
        (
            [ev(1, "created", 1)],
            state(version=1, foreign_after=True),
            ACTION_SKIP,
            "modified_since",
        ),
        # updated by the session
        ([ev(1, "updated", 3, {"summary": "before"})], state(version=3), ACTION_RESTORE, None),
        (
            [ev(1, "updated", 3, {"summary": "before"})],
            state(version=3, deleted=True, deleted_by="someone"),
            ACTION_SKIP,
            "deleted_by_other",
        ),
        (
            [ev(1, "updated", 3, {"summary": "now"})],
            state(version=3),
            ACTION_SKIP,
            "already_restored",
        ),  # changed and changed back: nothing to write
        # forgotten by the session
        (
            [ev(1, "forgotten", 4)],
            state(version=4, deleted=True, deleted_by=ME),
            ACTION_UNFORGET,
            None,
        ),
        (
            [ev(1, "forgotten", 4)],
            state(version=4, deleted=True, deleted_by="other"),
            ACTION_SKIP,
            "deleted_by_other",
        ),
        ([ev(1, "forgotten", 4)], state(version=4), ACTION_SKIP, "already_restored"),
        (
            [ev(1, "updated", 4, {"summary": "x"}), ev(2, "forgotten", 4)],
            state(version=4, deleted=True, deleted_by=ME),
            ACTION_RESTORE,
            None,
        ),
        # never touched
        ([ev(1, "created", 1)], MemoryState(exists=False), ACTION_SKIP, "purged"),
        ([ev(1, "created", 1)], state(version=1, resource_row=True), ACTION_SKIP, "resource_row"),
        ([ev(1, "created", 1)], state(version=1, permitted=False), ACTION_SKIP, "not_permitted"),
        (
            [ev(1, "updated", 2, {"details": None, "erased": ["details"]})],
            state(version=2),
            ACTION_SKIP,
            "prior_erased",
        ),
        (
            # erasure stripped the details the session overwrote (both markers)
            [ev(1, "updated", 2, {"erased": ["details"], "scrubbed": ["details"]})],
            state(version=2),
            ACTION_SKIP,
            "prior_erased",
        ),
        (
            # v0.113.0 review F3: the author was erased, but the session
            # changed only the tags — their prior is intact
            [ev(1, "updated", 2, {"tags": ["old"], "scrubbed": ["details"]})],
            state(version=2),
            ACTION_RESTORE,
            None,
        ),
    ],
)
def test_plan_memory_cause_table(events, st, action, cause):
    item = plan_memory(events, st)
    assert (item.action, item.cause) == (action, cause)
    assert item.event_ids == tuple(e.id for e in events)
    assert cause is None or cause in sr.SKIP_CAUSES


def test_restore_takes_each_field_from_the_sessions_first_edit_of_it():
    events = [
        ev(1, "updated", 2, {"summary": "orig", "tags": ["orig"]}),
        ev(2, "updated", 3, {"summary": "mid", "importance": 0.1}),
        ev(3, "updated", 4, {"tags": ["mid"]}),
    ]
    item = plan_memory(events, state(version=4))
    assert item.action == ACTION_RESTORE and not item.unforget
    assert item.values == {"summary": "orig", "tags": ["orig"], "importance": 0.1}
    assert item.kind == "updated"


def test_restore_after_forget_also_unforgets_the_sessions_tombstone():
    events = [ev(1, "updated", 2, {"summary": "orig"}), ev(2, "forgotten", 2)]
    item = plan_memory(events, state(version=2, deleted=True, deleted_by=ME))
    assert item.unforget and item.unforget_deleted_by == ME
    assert item.values == {"summary": "orig"}
    assert item.as_dict() == {
        "memory_id": str(MID),
        "kind": "forgotten",
        "action": "restore",
        "fields": ["summary"],
        "unforget": True,
    }


def test_combined_prior_keeps_null_and_ignores_marker_keys():
    values, erased = combined_prior(
        [ev(1, "updated", 2, {"details": None, "context_summary": None})]
    )
    assert values == {"context_summary": None, "details": None} and not erased
    values, erased = combined_prior([ev(1, "updated", 2, {"erased": ["details"], "type": "a"})])
    assert values == {"type": "a"} and erased
    # A field lost in a LATER edit is still restorable from the first one.
    values, erased = combined_prior(
        [ev(1, "updated", 2, {"details": {"k": 1}}), ev(2, "updated", 3, {"erased": ["details"]})]
    )
    assert values == {"details": {"k": 1}} and not erased


def test_scrubbed_blocks_only_a_field_the_event_changed():
    """v0.113.0 review F3: ``scrubbed`` (the live details of an erased author's
    memory are gone) is on every updated event of the memory; it must not stop
    a restore of fields whose prior is intact."""
    values, erased = combined_prior(
        [
            ev(1, "updated", 2, {"tags": ["orig"], "scrubbed": ["details"]}),
            ev(2, "updated", 3, {"importance": 0.2, "scrubbed": ["details"]}),
        ]
    )
    assert values == {"tags": ["orig"], "importance": 0.2} and not erased
    item = plan_memory(
        [ev(1, "updated", 2, {"tags": ["orig"], "scrubbed": ["details"]})],
        state(version=2, current={"tags": ["mine"]}),
    )
    assert (item.action, item.values) == (ACTION_RESTORE, {"tags": ["orig"]})


def test_tags_null_and_empty_are_the_same_value():
    item = plan_memory(
        [ev(1, "updated", 2, {"tags": None})], state(version=2, current={"tags": []})
    )
    assert (item.action, item.cause) == (ACTION_SKIP, "already_restored")


def test_plan_counts():
    items = [
        PlanItem(MID, "created", ACTION_FORGET),
        PlanItem(MID, "updated", ACTION_RESTORE),
        PlanItem(MID, "forgotten", ACTION_UNFORGET),
        PlanItem(MID, "created", ACTION_SKIP, cause="purged"),
        PlanItem(MID, "created", ACTION_SKIP, cause="purged"),
    ]
    assert plan_counts(items) == {"forget": 1, "restore": 1, "unforget": 1, "skip": 2}


# ---------------------------------------------------------------- executor


def _db() -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()

    @asynccontextmanager
    async def nested():
        yield

    db.begin_nested = MagicMock(side_effect=lambda: nested())
    return db


def _executor(db: MagicMock, memories: dict[uuid.UUID, Any]) -> Any:
    with patch("services.memory_service.MemoryService", MagicMock()):
        return sr._Executor(
            db,
            scope=sr.SessionScope(uuid.uuid4(), "s", None),
            workspace_id=uuid.uuid4(),
            requested_by=ME,
            rollback_id=7,
            memories=memories,
            classify=lambda exc: "service_unavailable",
            new_correlation_id=lambda: "cid-1",
        )


@pytest.mark.asyncio
async def test_a_failing_item_is_isolated_and_left_for_the_next_call():
    db = _db()
    good, bad = uuid.uuid4(), uuid.uuid4()
    executor = _executor(db, {})
    seen: list[uuid.UUID] = []

    async def restore(items: list[PlanItem]) -> None:
        seen.append(items[0].memory_id)
        if items[0].memory_id == bad:
            raise ConnectionError("down")
        executor.summary["updated_restored"] += 1

    executor._restore = restore  # type: ignore[method-assign]
    marked: list[list[int]] = []

    async def mark(_db: Any, ids: list[int], rollback_id: int) -> None:
        assert rollback_id == 7
        marked.append(list(ids))

    with patch.object(sr, "_mark_handled", mark):
        await executor.run(
            [
                PlanItem(bad, "updated", ACTION_RESTORE, values={"summary": "x"}, event_ids=(1,)),
                PlanItem(good, "updated", ACTION_RESTORE, values={"summary": "y"}, event_ids=(2,)),
                PlanItem(uuid.uuid4(), "created", ACTION_SKIP, cause="purged", event_ids=(3,)),
            ]
        )
    assert seen == [bad, good]  # the failure did not stop the run
    assert executor.summary["failed"] == 1 and executor.summary["updated_restored"] == 1
    assert executor.summary["skipped"] == 1
    assert executor.summary["errors"] == [
        f"Memory {bad} (restore) failed: service_unavailable (correlation_id cid-1)"
    ]
    assert executor.correlation_id == "cid-1"
    # The skipped and the good item are handled; the failed one is not, so the
    # next call plans it again.
    assert sorted(i for ids in marked for i in ids) == [2, 3]
    assert executor.failed_events == 1
    assert executor.restored_ids == [good]
    assert db.begin_nested.call_count == 2  # one SAVEPOINT per item


@pytest.mark.asyncio
async def test_forgets_go_in_chunks_each_in_its_own_savepoint():
    db = _db()
    executor = _executor(db, {})
    chunks: list[int] = []

    async def forget(items: list[PlanItem]) -> None:
        chunks.append(len(items))

    executor._forget_chunk = forget  # type: ignore[method-assign]
    executor._collection = AsyncMock(return_value="coll")  # type: ignore[method-assign]
    items = [
        PlanItem(uuid.uuid4(), "created", ACTION_FORGET, event_ids=(i,))
        for i in range(sr.FORGET_CHUNK + 5)
    ]
    with patch.object(sr, "_mark_handled", AsyncMock()):
        await executor.run(items)
    assert chunks == [sr.FORGET_CHUNK, 5]
    assert db.begin_nested.call_count == 2


@pytest.mark.asyncio
async def test_a_dry_run_locks_nothing_and_writes_nothing():
    db = _db()
    batch = sr._Batch(
        items=[PlanItem(MID, "created", ACTION_FORGET, event_ids=(1,))],
        memories={},
        events=1,
        remaining=0,
    )
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))
    with (
        patch.object(sr, "_session_exists", AsyncMock(return_value=True)),
        patch.object(sr, "_read_batch", AsyncMock(return_value=batch)) as read,
        patch.object(sr, "_lock_session", AsyncMock()) as lock,
    ):
        result = await sr.rollback_session(
            db,
            scope=sr.SessionScope(uuid.uuid4(), "s", None),
            workspace_id=uuid.uuid4(),
            requested_by=ME,
            dry_run=True,
            classify=lambda e: "x",
            new_correlation_id=lambda: "c",
        )
    assert result.dry_run and result.items == batch.items and result.rollback_id is None
    lock.assert_not_awaited()
    assert read.await_args.kwargs["lock"] is False
    db.add.assert_not_called()
    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_unknown_session_is_not_found_before_anything_else():
    db = _db()
    with patch.object(sr, "_session_exists", AsyncMock(return_value=False)):
        with pytest.raises(sr.SessionNotFoundError):
            await sr.rollback_session(
                db,
                scope=sr.SessionScope(uuid.uuid4(), "s", sr.SessionPrincipal(ME, None)),
                workspace_id=uuid.uuid4(),
                requested_by=ME,
                dry_run=True,
                classify=lambda e: "x",
                new_correlation_id=lambda: "c",
            )


def _empty_batch() -> Any:
    return sr._Batch(items=[], memories={}, events=0, remaining=0)


async def _run(db: MagicMock, record: Any, batch: Any, *, dry_run: bool = False) -> Any:
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: record))
    with (
        patch.object(sr, "_session_exists", AsyncMock(return_value=True)),
        patch.object(sr, "_lock_session", AsyncMock()),
        patch.object(sr, "_read_batch", AsyncMock(return_value=batch)),
    ):
        return await sr.rollback_session(
            db,
            scope=sr.SessionScope(uuid.uuid4(), "s", None),
            workspace_id=uuid.uuid4(),
            requested_by=ME,
            dry_run=dry_run,
            classify=lambda e: "x",
            new_correlation_id=lambda: "c",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_a_completed_rollback_with_nothing_new_refuses_the_next_call(dry_run):
    record = MagicMock(id=11, status="completed")
    with pytest.raises(sr.AlreadyRolledBackError) as refused:
        await _run(_db(), record, _empty_batch(), dry_run=dry_run)
    assert (refused.value.rollback_id, refused.value.status) == (11, "completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "partial", "in_progress"])
async def test_new_or_failed_events_reopen_the_rollback(status):
    """A completed rollback whose session wrote again, and a partial one
    whose failed items wait, are continued — under the same record."""
    record = MagicMock(id=11, status=status, summary={"failed": 1, "calls": 1})
    batch = sr._Batch(
        items=[PlanItem(MID, "created", ACTION_FORGET, event_ids=(5,))],
        memories={},
        events=1,
        remaining=0,
    )
    executor = MagicMock(
        run=AsyncMock(),
        summary=sr._empty_summary(),
        failed_events=0,
        correlation_id=None,
        reembed_ids=[],
        restored_ids=[],
        forgotten_ids=[MID],
        forgotten_points=[str(MID)],
        collection="coll",
    )
    executor.summary["forgotten"] = 1
    with patch.object(sr, "_Executor", MagicMock(return_value=executor)):
        result = await _run(_db(), record, batch)
    assert result.rollback_id == 11 and record.status == "completed"
    assert result.totals["calls"] == 2 and result.totals["failed"] == 1
    assert result.forgotten_points == [str(MID)] and result.audit is not None


@pytest.mark.asyncio
async def test_failed_items_keep_the_rollback_open_as_partial():
    record = MagicMock(id=3, status="in_progress", summary={})
    batch = sr._Batch(
        items=[PlanItem(MID, "forgotten", ACTION_UNFORGET, unforget=True, event_ids=(1, 2))],
        memories={},
        events=2,
        remaining=4,
    )
    executor = MagicMock(
        run=AsyncMock(),
        summary={**sr._empty_summary(), "failed": 1},
        failed_events=2,
        correlation_id="cid",
        reembed_ids=[],
        restored_ids=[],
        forgotten_ids=[],
        forgotten_points=[],
        collection=None,
    )
    with patch.object(sr, "_Executor", MagicMock(return_value=executor)):
        result = await _run(_db(), record, batch)
    assert record.status == "partial"
    assert (result.remaining, result.events) == (6, 0)


@pytest.mark.asyncio
async def test_finish_deletes_points_and_audits_after_the_commit():
    deleted = [uuid.uuid4(), uuid.uuid4()]
    result = sr.RollbackResult(
        dry_run=False,
        items=[],
        events=3,
        remaining=0,
        rollback_id=4,
        restored_ids=[MID],
        forgotten_ids=deleted,
        forgotten_points=["p1", "p2"],
        collection="coll",
        audit=(uuid.uuid4(), ME, uuid.uuid4()),
    )
    with (
        patch(
            "db.qdrant.delete_points_from_qdrant", AsyncMock(side_effect=OSError("down"))
        ) as points,
        patch("db.qdrant.delete_chunk_points", AsyncMock()) as chunks,
        patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()) as emit,
    ):
        await sr.finish(result)  # a failed vector delete does not raise
    points.assert_awaited_once_with(["p1", "p2"], "coll")
    chunks.assert_awaited_once_with(deleted, "coll")
    calls = {c.kwargs["operation"]: c.kwargs for c in emit.await_args_list}
    assert calls["forget"]["result_count"] == 2
    assert calls["forget"]["extra_metadata"]["memory_ids"] == [str(m) for m in deleted]
    assert calls["update"]["memory_id"] == MID
    assert calls["update"]["extra_metadata"] == {"rollback_id": 4}


def test_principal_predicates_compile_index_friendly():
    from sqlalchemy.dialects import postgresql

    def sql(scope: sr.SessionScope) -> str:
        from sqlalchemy import and_

        return str(
            and_(*sr._api_event_predicates(scope)).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )

    ctx = uuid.uuid4()
    agent = uuid.uuid4()
    user = sql(sr.SessionScope(ctx, "s", sr.SessionPrincipal("u1", None)))
    assert "memory_revisions.actor_user_id = 'u1'" in user
    assert "memory_revisions.agent_id IS NULL" in user
    assert "memory_revisions.writer = 'api'" in user
    bound = sql(sr.SessionScope(ctx, "s", sr.SessionPrincipal("u1", agent)))
    assert "memory_revisions.agent_id = " in bound
    owner = sql(sr.SessionScope(ctx, "s", None))
    assert "actor_user_id" not in owner and "agent_id" not in owner
    assert "memory_revisions.session_id = 's'" in owner


def test_unforgets_beyond_the_per_call_cap_wait_for_the_next_call():
    items = [
        PlanItem(uuid.uuid4(), "forgotten", ACTION_UNFORGET, unforget=True, event_ids=(i,))
        for i in range(3)
    ] + [PlanItem(uuid.uuid4(), "created", ACTION_FORGET, event_ids=(9,))]
    with patch.object(sr, "MAX_UNFORGETS_PER_CALL", 2):
        kept = sr._defer_extra_unforgets(items)
    assert kept == [items[0], items[1], items[3]]


@pytest.mark.asyncio
async def test_finish_reembeds_one_after_another_and_survives_a_failure():
    import asyncio

    order: list[uuid.UUID] = []
    ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]

    async def embed(memory_id: uuid.UUID) -> None:
        order.append(memory_id)
        if memory_id == ids[0]:
            raise RuntimeError("provider down")

    result = sr.RollbackResult(dry_run=False, items=[], events=0, remaining=0, reembed_ids=ids)
    with patch("services.memory_service.process_pending_embedding", embed):
        await sr.finish(result)
        for _ in range(10):
            await asyncio.sleep(0)
    assert order == ids


# ------------------------------------------- v0.113.0 review: F1 / F2


@pytest.mark.asyncio
async def test_a_spent_time_budget_defers_the_rest_to_the_next_call():
    """F2: once the budget is spent no further item starts; the first always
    runs (each call progresses) and the rest stay unhandled."""
    import time

    db = _db()
    executor = _executor(db, {})
    executor.deadline = time.monotonic() - 1  # already spent
    ran: list[uuid.UUID] = []

    async def restore(items: list[PlanItem]) -> None:
        ran.append(items[0].memory_id)

    executor._restore = restore  # type: ignore[method-assign]
    items = [
        PlanItem(uuid.uuid4(), "forgotten", ACTION_UNFORGET, unforget=True, event_ids=(i,))
        for i in range(3)
    ] + [PlanItem(uuid.uuid4(), "created", ACTION_SKIP, cause="purged", event_ids=(9,))]
    marked: list[int] = []

    async def mark(_db: Any, ids: list[int], _rid: int) -> None:
        marked.extend(ids)

    with patch.object(sr, "_mark_handled", mark):
        await executor.run(items)
    assert ran == [items[0].memory_id]
    assert executor.deferred == items[1:3]
    assert sorted(marked) == [0, 9]  # the skip and the one item that ran


@pytest.mark.asyncio
async def test_deferred_items_remain_and_keep_the_rollback_in_progress():
    record = MagicMock(id=3, status="in_progress", summary={})
    done, later = uuid.uuid4(), uuid.uuid4()
    batch = sr._Batch(
        items=[
            PlanItem(done, "created", ACTION_FORGET, event_ids=(1,)),
            PlanItem(later, "forgotten", ACTION_UNFORGET, unforget=True, event_ids=(2, 3)),
        ],
        memories={},
        events=3,
        remaining=0,
    )
    executor = MagicMock(
        run=AsyncMock(),
        summary=sr._empty_summary(),
        failed_events=0,
        correlation_id=None,
        reembed_ids=[],
        restored_ids=[],
        forgotten_ids=[done],
        forgotten_points=[],
        collection="coll",
        payload_syncs=[],
        deferred=[batch.items[1]],
    )
    with patch.object(sr, "_Executor", MagicMock(return_value=executor)):
        result = await _run(_db(), record, batch)
    assert (result.status, result.events, result.remaining) == ("in_progress", 1, 2)
    assert [i.memory_id for i in result.items] == [done]


def _restored_memory() -> Any:
    from types import SimpleNamespace

    from utils.datetime import utcnow

    return SimpleNamespace(
        id=uuid.uuid4(),
        summary="s",
        context_summary=None,
        content="c",
        details=None,
        type="note",
        importance=0.9,
        tags=["session"],
        version=2,
        updated_at=utcnow(),
        embedding_status="success",
        context_id=uuid.uuid4(),
        agent_id=None,
        session_id=None,
    )


@pytest.mark.asyncio
async def test_a_metadata_restore_patches_the_payload_only_after_the_commit():
    """F1: the restore queues the payload patch; nothing reaches the vector
    store inside the transaction, and a failed item queues nothing."""
    db = _db()
    good, bad = _restored_memory(), _restored_memory()
    executor = _executor(db, {good.id: good, bad.id: bad})
    executor._collection = AsyncMock(return_value="coll")  # type: ignore[method-assign]
    items = [
        PlanItem(
            m.id,
            "updated",
            ACTION_RESTORE,
            values={"tags": ["orig"], "importance": 0.5},
            event_ids=(n,),
        )
        for n, m in enumerate((good, bad), start=1)
    ]

    async def mark(_db: Any, ids: list[int], _rid: int) -> None:
        if ids == [2]:
            raise ConnectionError("lost")

    with (
        patch("db.qdrant.update_memory_payload_in_qdrant", AsyncMock()) as qdrant,
        patch("services.content_chunk_writer.chunk_cascade_needed", AsyncMock(return_value=True)),
        patch.object(sr, "record_edit", MagicMock()),
        patch.object(sr, "_mark_handled", mark),
    ):
        await executor.run(items)
    qdrant.assert_not_awaited()
    (sync,) = executor.payload_syncs
    assert sync.memory_id == good.id and sync.collection == "coll" and sync.cascade_chunks
    assert sync.updates["tags"] == ["orig"] and sync.updates["importance"] == 0.5
    assert "updated_at" in sync.updates and "type" not in sync.updates
    assert sync.fields == ("importance", "tags")


@pytest.mark.asyncio
async def test_finish_applies_payload_syncs_and_survives_a_failure():
    first, second = uuid.uuid4(), uuid.uuid4()
    syncs = [
        sr.PayloadSync(
            m, "coll", ("tags",), {"tags": ["a"]}, ("location",) if m == first else (), False
        )
        for m in (first, second)
    ]
    result = sr.RollbackResult(dry_run=False, items=[], events=0, remaining=0, payload_syncs=syncs)
    calls: list[dict[str, Any]] = []

    async def patch_payload(**kwargs: Any) -> None:
        calls.append(kwargs)
        if kwargs["memory_id"] == first:
            raise OSError("vector store down")

    with patch("db.qdrant.update_memory_payload_in_qdrant", patch_payload):
        await sr.finish(result)  # a failed patch does not raise
    assert [c["memory_id"] for c in calls] == [first, second]
    assert calls[0]["delete_keys"] == ["location"] and calls[1]["delete_keys"] is None
    assert calls[1]["collection_name"] == "coll" and calls[1]["cascade_chunks"] is False
