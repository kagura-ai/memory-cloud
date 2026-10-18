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
            [ev(1, "updated", 2, {"scrubbed": ["details"]})],
            state(version=2),
            ACTION_SKIP,
            "prior_erased",
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
async def test_a_failing_item_is_isolated_and_still_marked_handled():
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
    # skipped first, then the failed item (outside its SAVEPOINT), then the good one
    assert sorted(i for ids in marked for i in ids) == [1, 2, 3]
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


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "partial"])
async def test_a_final_rollback_record_refuses_the_next_call(status):
    db = _db()
    record = MagicMock(id=11, status=status)
    db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: record))
    with (
        patch.object(sr, "_session_exists", AsyncMock(return_value=True)),
        patch.object(sr, "_lock_session", AsyncMock()),
        patch.object(sr, "_read_batch", AsyncMock()) as read,
    ):
        with pytest.raises(sr.AlreadyRolledBackError) as refused:
            await sr.rollback_session(
                db,
                scope=sr.SessionScope(uuid.uuid4(), "s", None),
                workspace_id=uuid.uuid4(),
                requested_by=ME,
                dry_run=False,
                classify=lambda e: "x",
                new_correlation_id=lambda: "c",
            )
    assert (refused.value.rollback_id, refused.value.status) == (11, status)
    read.assert_not_awaited()


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
