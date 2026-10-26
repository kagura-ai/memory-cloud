"""#2283: the shared post-commit payload step and its two failure policies."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


async def test_swallow_plans_without_rows_after_a_failed_read() -> None:
    (only,) = _ids(1)
    db = _db()
    seen: list[Any] = []

    def plan(memory_id: uuid.UUID, rows: Mapping[uuid.UUID, Any] | None) -> Any:
        seen.append(rows)
        return ps.PlannedPatch(memory_id, None)

    with patch(_READ, AsyncMock(side_effect=RuntimeError("read"))):
        await ps.sync_payloads_after_commit(
            db, [only], memory_id=lambda i: i, plan=plan, apply=AsyncMock(),
            on_failure=ps.SwallowFailures("p"),
        )  # fmt: skip
    assert seen == [None]
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()  # nothing read, nothing to release


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
    policy = ps.RequeueEmbeddingOnFailure("erasure_details_payload", reason="r")
    with patch(_READ, AsyncMock(side_effect=RuntimeError("read"))):
        await ps.sync_payloads_after_commit(
            db, ids, memory_id=lambda i: i, plan=_plan_all(), apply=apply, on_failure=policy
        )
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
    event = log.warning.call_args.args[0]
    if kind == "swallow":
        assert event == "p_tx_end_failed"
        db.rollback.assert_not_awaited()
    else:
        assert event == "p_commit_failed"
        db.rollback.assert_awaited_once()


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
