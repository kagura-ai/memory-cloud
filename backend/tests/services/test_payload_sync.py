"""#2283: the shared post-commit payload step and its two failure policies."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from services import payload_sync as ps
from utils.exceptions import QdrantError

pytestmark = pytest.mark.asyncio

_READ = "services.payload_sync.share_lock_rows"
_REQUEUE = "services.content_chunk_writer.requeue_embedding"


def _db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _ids(n: int) -> list[uuid.UUID]:
    return sorted(uuid.uuid4() for _ in range(n))


def _plan_all(
    embedded: set[uuid.UUID] | None = None,
) -> Any:
    def plan(memory_id: uuid.UUID, rows: Mapping[uuid.UUID, Any] | None) -> Any:
        return ps.PlannedPatch(
            memory_id, rows, embedded=memory_id in (embedded or set()), log_fields={"x": "y"}
        )

    return plan


def _applier(failing: set[uuid.UUID], exc: Exception) -> tuple[list[uuid.UUID], Any]:
    applied: list[uuid.UUID] = []

    async def apply(patch: ps.PlannedPatch[Any]) -> None:
        applied.append(patch.memory_id)
        if patch.memory_id in failing:
            raise exc

    return applied, apply


async def test_swallow_logs_a_failed_patch_and_goes_on() -> None:
    first, second = _ids(2)
    applied, apply = _applier({first}, QdrantError("down"))
    policy = ps.SwallowFailures("session_rollback_payload")
    with patch.object(ps, "logger") as log:
        await ps.sync_payloads_after_commit(
            None, [first, second], memory_id=lambda i: i, plan=_plan_all(), apply=apply,
            on_failure=policy,
        )  # fmt: skip
    assert applied == [first, second]
    assert policy.failed == 1
    log.warning.assert_called_once_with(
        "session_rollback_payload_sync_failed",
        memory_id=str(first),
        x="y",
        error_class="QdrantError",
    )


async def test_swallow_never_requeues_an_embedded_row() -> None:
    (only,) = _ids(1)
    db = _db()
    _, apply = _applier({only}, QdrantError("down"))
    requeue = AsyncMock(return_value=True)
    with patch(_READ, AsyncMock(return_value={only: object()})), patch(_REQUEUE, requeue):
        await ps.sync_payloads_after_commit(
            db, [only], memory_id=lambda i: i, plan=_plan_all({only}), apply=apply,
            on_failure=ps.SwallowFailures("p"),
        )  # fmt: skip
    requeue.assert_not_awaited()
    db.commit.assert_awaited_once()


async def test_swallow_never_patches_a_row_that_stays_unreadable() -> None:
    """#2346: no patch from the values queued at the restore — an edit
    committed since would have its payload overwritten with them. #2433: the
    group's rows are read again one at a time; none of them can be read, so
    none is planned."""
    ids = _ids(3)
    db = _db()
    plan = MagicMock(side_effect=_plan_all())
    apply = AsyncMock()
    read = AsyncMock(side_effect=RuntimeError("read"))
    policy = ps.SwallowFailures("p")
    with patch(_READ, read), patch.object(ps, "logger") as log:
        await ps.sync_payloads_after_commit(
            db, ids, memory_id=lambda i: i, plan=plan, apply=apply, on_failure=policy
        )
    assert [list(c.args[1]) for c in read.await_args_list] == [ids, *([i] for i in ids)]
    plan.assert_not_called()
    apply.assert_not_awaited()
    assert (policy.failed, policy.unread, policy.recovered) == (0, 3, 0)
    assert db.rollback.await_count == 4  # the group's read, then each row's
    db.commit.assert_not_awaited()  # nothing read, nothing to release
    assert log.warning.call_args_list == [
        call("p_reread_failed", memory_ids=[str(i)], error_class="RuntimeError") for i in ids
    ]


class _AbortingSession:
    """A session whose failed statement leaves the transaction aborted until
    it is rolled back, as a real one does (a failed read sets ``aborted``)."""

    def __init__(self, fail_commits: int) -> None:
        self.fail_commits = fail_commits
        self.aborted = False
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        if self.aborted:
            raise RuntimeError("transaction is aborted")
        if self.fail_commits:
            self.fail_commits -= 1
            self.aborted = True
            raise RuntimeError("commit")
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.aborted = False


def _lock_timeout() -> OperationalError:
    return OperationalError(
        "SELECT ... FOR SHARE", {}, Exception("canceling statement due to lock timeout")
    )


async def test_swallow_reads_a_failed_groups_rows_one_at_a_time() -> None:
    """#2433: a lock timeout on one row fails its group's read. The group is
    rolled back and its rows are read again one at a time, each in a
    transaction of its own: the other rows are planned from their own fresh
    read and patched; the locked row is never planned."""
    first, locked, last = ids = _ids(3)
    db = _AbortingSession(fail_commits=0)
    reads: list[list[uuid.UUID]] = []

    async def read(session: Any, group: list[uuid.UUID], **_: Any) -> dict[uuid.UUID, Any]:
        if session.aborted:
            raise RuntimeError("current transaction is aborted")
        reads.append(list(group))
        if locked in group:
            session.aborted = True
            raise _lock_timeout()
        return {i: SimpleNamespace(id=i) for i in group}

    planned: list[tuple[uuid.UUID, Mapping[uuid.UUID, Any] | None]] = []

    def plan(memory_id: uuid.UUID, rows: Mapping[uuid.UUID, Any] | None) -> Any:
        planned.append((memory_id, rows))
        return ps.PlannedPatch(memory_id, rows)

    applied, apply = _applier(set(), RuntimeError())
    policy = ps.SwallowFailures("p")
    with patch(_READ, side_effect=read), patch.object(ps, "logger") as log:
        await ps.sync_payloads_after_commit(
            cast(Any, db), ids, memory_id=lambda i: i, plan=plan, apply=apply,
            on_failure=policy,
        )  # fmt: skip
    assert reads == [ids, [first], [locked], [last]]
    # each row from its own read, never from the failed group's (or None)
    assert [(m, set(rows or ())) for m, rows in planned] == [(first, {first}), (last, {last})]
    assert applied == [first, last]
    assert (policy.failed, policy.unread, policy.recovered) == (0, 1, 2)
    assert (db.rollbacks, db.commits) == (2, 2)  # the group's and the locked row's reads
    log.warning.assert_called_once_with(
        "p_reread_failed", memory_ids=[str(locked)], error_class="OperationalError"
    )


_LOST_CONNECTIONS = [
    ConnectionRefusedError("refused"),
    TimeoutError("connect timed out"),
    PoolTimeoutError("QueuePool limit reached"),
    InterfaceError("SELECT", {}, Exception("connection is closed")),
    DBAPIError("SELECT", {}, Exception("terminated"), connection_invalidated=True),
]


@pytest.mark.parametrize("exc", _LOST_CONNECTIONS, ids=lambda e: type(e).__name__)
async def test_swallow_reads_no_row_on_its_own_after_a_lost_connection(exc: Exception) -> None:
    """#2433: a group read that failed on the connection, not on a row, is
    not retried row by row: each row's read would fail (and wait) the same
    way. The group is left unread, as before #2433."""
    ids = _ids(3)
    db = _db()
    read = AsyncMock(side_effect=exc)
    policy = ps.SwallowFailures("p")
    with patch(_READ, read), patch.object(ps, "logger") as log:
        await ps.sync_payloads_after_commit(
            db, ids, memory_id=lambda i: i, plan=_plan_all(), apply=AsyncMock(),
            on_failure=policy,
        )  # fmt: skip
    read.assert_awaited_once()
    assert (policy.failed, policy.unread, policy.recovered) == (0, 3, 0)
    db.rollback.assert_awaited_once()
    log.warning.assert_called_once_with(
        "p_reread_failed", memory_ids=[str(i) for i in ids], error_class=type(exc).__name__
    )


async def test_swallow_reads_no_row_on_its_own_after_a_failed_rollback() -> None:
    """#2433: a group whose rollback fails after its read failed is not
    retried row by row either."""
    ids = _ids(3)
    db = _db()
    db.rollback = AsyncMock(side_effect=RuntimeError("connection gone"))
    read = AsyncMock(side_effect=_lock_timeout())
    policy = ps.SwallowFailures("p")
    with patch(_READ, read), patch.object(ps, "logger") as log:
        await ps.sync_payloads_after_commit(
            db, ids, memory_id=lambda i: i, plan=_plan_all(), apply=AsyncMock(),
            on_failure=policy,
        )  # fmt: skip
    read.assert_awaited_once()
    assert (policy.unread, policy.recovered) == (3, 0)
    assert [c.args[0] for c in log.warning.call_args_list] == ["p_tx_end_failed", "p_reread_failed"]


async def test_swallow_stops_reading_rows_once_the_connection_is_lost() -> None:
    """#2433: the retry stops at a row whose own read fails on the
    connection; that row and the rest of the group are left unread, in one
    log line, without a read each."""
    first, second, third, fourth = ids = _ids(4)
    db = _db()
    reads: list[list[uuid.UUID]] = []

    async def read(_db: Any, group: list[uuid.UUID], **_: Any) -> dict[uuid.UUID, Any]:
        reads.append(list(group))
        if len(group) > 1:
            raise _lock_timeout()
        if group == [second]:
            raise ConnectionRefusedError("refused")
        return {i: SimpleNamespace(id=i) for i in group}

    applied, apply = _applier(set(), RuntimeError())
    policy = ps.SwallowFailures("p")
    with patch(_READ, side_effect=read), patch.object(ps, "logger") as log:
        await ps.sync_payloads_after_commit(
            db, ids, memory_id=lambda i: i, plan=_plan_all(), apply=apply, on_failure=policy
        )
    assert reads == [ids, [first], [second]]
    assert applied == [first]
    assert (policy.failed, policy.unread, policy.recovered) == (0, 3, 1)
    log.warning.assert_called_once_with(
        "p_reread_failed",
        memory_ids=[str(i) for i in (second, third, fourth)],
        error_class="ConnectionRefusedError",
    )


async def test_swallow_rolls_back_a_failed_commit_so_the_next_group_reads() -> None:
    """#2346: the next group's ``FOR SHARE`` read works after a failed commit."""
    ids = _ids(2)
    db = _AbortingSession(fail_commits=1)
    reads: list[list[uuid.UUID]] = []

    async def read(session: Any, group: list[uuid.UUID], **_: Any) -> dict[uuid.UUID, Any]:
        if session.aborted:
            raise RuntimeError("current transaction is aborted")
        reads.append(list(group))
        return {i: SimpleNamespace() for i in group}

    seen: list[Any] = []

    def plan(memory_id: uuid.UUID, rows: Mapping[uuid.UUID, Any] | None) -> Any:
        seen.append(rows)
        return ps.PlannedPatch(memory_id, rows)

    applied, apply = _applier(set(), RuntimeError())
    policy = ps.SwallowFailures("p")
    with patch.object(ps, "PAYLOAD_SYNC_GROUP", 1), patch(_READ, side_effect=read):
        await ps.sync_payloads_after_commit(
            cast(Any, db), ids, memory_id=lambda i: i, plan=plan, apply=apply,
            on_failure=policy,
        )  # fmt: skip
    assert reads == [ids[:1], ids[1:]]
    assert None not in seen  # never planned from the queued values
    assert applied == ids
    assert db.rollbacks == 1
    assert (policy.failed, policy.unread) == (0, 0)


async def test_requeue_degrades_a_failed_patch_to_an_embedding_requeue() -> None:
    embedded, queued, broken, fine = _ids(4)
    db = _db()
    applied, apply = _applier({embedded, queued, broken}, QdrantError("down"))
    policy = ps.RequeueEmbeddingOnFailure("erasure_details_payload", reason="r")

    async def requeue(_db: Any, memory_id: uuid.UUID, *, reason: str) -> bool:
        assert reason == "r"
        if memory_id == broken:
            raise RuntimeError("requeue")
        return True

    rows = {i: object() for i in (embedded, queued, broken, fine)}
    with patch(_READ, AsyncMock(return_value=rows)), patch(_REQUEUE, side_effect=requeue) as rq:
        await ps.sync_payloads_after_commit(
            db, [embedded, queued, broken, fine], memory_id=lambda i: i,
            plan=_plan_all({embedded, broken}), apply=apply, on_failure=policy,
        )  # fmt: skip
    assert applied == [embedded, queued, broken, fine]
    assert [c.args[1] for c in rq.await_args_list] == [embedded, broken]
    assert (policy.requeued_after_error, policy.skipped_requeued, policy.requeue_failed) == (
        1,
        1,
        1,
    )
    db.commit.assert_awaited_once()


async def test_requeue_skips_a_group_whose_read_failed() -> None:
    ids = _ids(3)
    db = _db()
    apply = AsyncMock()
    read = AsyncMock(side_effect=RuntimeError("read"))
    policy = ps.RequeueEmbeddingOnFailure("erasure_details_payload", reason="r")
    with patch(_READ, read):
        await ps.sync_payloads_after_commit(
            db, ids, memory_id=lambda i: i, plan=_plan_all(), apply=apply, on_failure=policy
        )
    read.assert_awaited_once()  # its rows are not read again one at a time (#2433)
    apply.assert_not_awaited()
    assert policy.requeue_failed == 3
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


async def test_groups_follow_the_given_order_and_commit_one_by_one() -> None:
    ids = _ids(3)
    db = _db()
    reads: list[list[uuid.UUID]] = []

    async def read(_db: Any, group: list[uuid.UUID], **_: Any) -> dict[uuid.UUID, Any]:
        reads.append(list(group))
        return {i: SimpleNamespace() for i in group}

    given = [ids[2], ids[0], ids[1]]
    with patch.object(ps, "PAYLOAD_SYNC_GROUP", 2), patch(_READ, side_effect=read):
        await ps.sync_payloads_after_commit(
            db, given, memory_id=lambda i: i, plan=_plan_all(), apply=AsyncMock(),
            on_failure=ps.SwallowFailures("p"),
        )  # fmt: skip
    assert reads == [given[:2], given[2:]]
    assert db.commit.await_count == 2


@pytest.mark.parametrize("kind", ["swallow", "requeue"])
async def test_a_failed_commit_goes_to_the_policy(kind: str) -> None:
    (only,) = _ids(1)
    db = _db()
    db.commit = AsyncMock(side_effect=RuntimeError("commit"))
    policy: ps.PayloadFailurePolicy = (
        ps.SwallowFailures("p")
        if kind == "swallow"
        else ps.RequeueEmbeddingOnFailure("p", reason="r")
    )
    with patch(_READ, AsyncMock(return_value={only: object()})), patch.object(ps, "logger") as log:
        await ps.sync_payloads_after_commit(
            db, [only], memory_id=lambda i: i, plan=_plan_all(), apply=AsyncMock(),
            on_failure=policy,
        )  # fmt: skip
    log.warning.assert_called_once_with(
        "p_commit_failed", memory_ids=[str(only)], error_class="RuntimeError"
    )
    db.rollback.assert_awaited_once()  # #2346: the swallow policy rolls back too


async def test_requeue_without_a_session_counts_an_embedded_row_as_failed() -> None:
    (only,) = _ids(1)
    _, apply = _applier({only}, QdrantError("down"))
    policy = ps.RequeueEmbeddingOnFailure("p", reason="r")
    with patch(_REQUEUE, AsyncMock(return_value=True)) as requeue:
        await ps.sync_payloads_after_commit(
            None, [only], memory_id=lambda i: i, plan=_plan_all({only}), apply=apply,
            on_failure=policy,
        )  # fmt: skip
    requeue.assert_not_awaited()
    assert (policy.requeued_after_error, policy.skipped_requeued, policy.requeue_failed) == (
        0,
        0,
        1,
    )


@pytest.mark.parametrize("kind", ["swallow", "requeue"])
async def test_log_fields_never_clash_with_the_policy_keys(kind: str) -> None:
    (only,) = _ids(1)
    _, apply = _applier({only}, RuntimeError("down"))

    def plan(memory_id: uuid.UUID, rows: Mapping[uuid.UUID, Any] | None) -> Any:
        fields = {"memory_id": "x", "error_class": "x", "requeued": "x", "collection": "c"}
        return ps.PlannedPatch(memory_id, None, log_fields=fields)

    policy: ps.PayloadFailurePolicy = (
        ps.SwallowFailures("p")
        if kind == "swallow"
        else ps.RequeueEmbeddingOnFailure("p", reason="r")
    )
    with patch.object(ps, "logger") as log:
        await ps.sync_payloads_after_commit(
            None, [only], memory_id=lambda i: i, plan=plan, apply=apply, on_failure=policy
        )
    kwargs = log.warning.call_args.kwargs
    assert kwargs["memory_id"] == str(only) and kwargs["error_class"] == "RuntimeError"
    assert kwargs["collection"] == "c"


async def test_requeue_survives_a_failed_rollback_after_a_failed_read() -> None:
    ids = _ids(3)
    db = _db()
    db.rollback = AsyncMock(side_effect=RuntimeError("connection gone"))
    reads: list[list[uuid.UUID]] = []

    async def read(_db: Any, group: list[uuid.UUID], **_: Any) -> dict[uuid.UUID, Any]:
        reads.append(list(group))
        raise RuntimeError("read")

    apply = AsyncMock()
    policy = ps.RequeueEmbeddingOnFailure("p", reason="r")
    with (
        patch.object(ps, "PAYLOAD_SYNC_GROUP", 2),
        patch(_READ, side_effect=read),
        patch.object(ps, "logger") as log,
    ):
        await ps.sync_payloads_after_commit(
            db, ids, memory_id=lambda i: i, plan=_plan_all(), apply=apply, on_failure=policy
        )  # does not raise
    assert reads == [ids[:2], ids[2:]]  # the next group still runs
    apply.assert_not_awaited()
    assert policy.requeue_failed == 3
    events = [c.args[0] for c in log.warning.call_args_list]
    assert events == ["p_tx_end_failed", "p_read_failed"] * 2


async def test_requeue_survives_a_failed_rollback_after_a_failed_commit() -> None:
    ids = _ids(2)
    db = _db()
    db.commit = AsyncMock(side_effect=RuntimeError("commit"))
    db.rollback = AsyncMock(side_effect=RuntimeError("connection gone"))
    applied: list[uuid.UUID] = []

    async def apply(patch: ps.PlannedPatch[Any]) -> None:
        applied.append(patch.memory_id)

    policy = ps.RequeueEmbeddingOnFailure("p", reason="r")
    with (
        patch.object(ps, "PAYLOAD_SYNC_GROUP", 1),
        patch(_READ, AsyncMock(side_effect=lambda _db, g, **_: {i: object() for i in g})),
        patch.object(ps, "logger") as log,
    ):
        await ps.sync_payloads_after_commit(
            db, ids, memory_id=lambda i: i, plan=_plan_all(), apply=apply, on_failure=policy
        )  # does not raise
    assert applied == ids  # the next group still runs
    assert (policy.requeued_after_error, policy.skipped_requeued, policy.requeue_failed) == (
        0,
        0,
        0,
    )
    events = [c.args[0] for c in log.warning.call_args_list]
    assert events == ["p_tx_end_failed", "p_commit_failed"] * 2
    assert log.warning.call_args_list[1].kwargs["memory_ids"] == [str(ids[0])]


@pytest.mark.parametrize("step", ["read", "commit"])
async def test_swallow_survives_a_failed_rollback(step: str) -> None:
    """A rollback that fails after a failed read or commit is logged, never
    raised, and the next group still runs."""
    ids = _ids(2)
    db = _db()
    db.rollback = AsyncMock(side_effect=RuntimeError("connection gone"))
    if step == "commit":
        db.commit = AsyncMock(side_effect=RuntimeError("commit"))
        read = AsyncMock(side_effect=lambda _db, g, **_: {i: object() for i in g})
    else:
        read = AsyncMock(side_effect=RuntimeError("read"))
    policy = ps.SwallowFailures("p")
    with (
        patch.object(ps, "PAYLOAD_SYNC_GROUP", 1),
        patch(_READ, read),
        patch.object(ps, "logger") as log,
    ):
        await ps.sync_payloads_after_commit(
            db, ids, memory_id=lambda i: i, plan=_plan_all(), apply=AsyncMock(),
            on_failure=policy,
        )  # fmt: skip
    assert read.await_count == 2  # the next group still runs; a one-row group is not read again
    event = "p_reread_failed" if step == "read" else "p_commit_failed"
    assert [c.args[0] for c in log.warning.call_args_list] == ["p_tx_end_failed", event] * 2
    assert policy.unread == (2 if step == "read" else 0)
