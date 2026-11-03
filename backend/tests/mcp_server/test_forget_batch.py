"""forget(memory_ids=[...]) — the single forget applied per id (#2127).

The handler calls ``MemoryService.forget`` once per id, exactly as the
single form does, and reports one row per id: ``deleted`` / ``skipped`` /
``error``. The service commits each id itself, so a failing id is rolled
back alone. One usage row and one context touch cover the whole call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from mcp_server.tools.memory import FORGET_MAX_IDS, handle_forget
from utils.exceptions import AuthorizationError


def _context():
    return SimpleNamespace(id=uuid4(), name="ctx", last_used_at=None, workspace_id=uuid4())


@contextlib.contextmanager
def _patched(service, context=None, forgotten=False):
    db = AsyncMock()

    async def mock_get_db():
        yield db

    log = AsyncMock()
    touch = AsyncMock(return_value=True)
    with (
        patch("db.base.get_db", new=mock_get_db),
        patch(
            "mcp_server.tools.memory._resolve_context",
            new=AsyncMock(return_value=context or _context()),
        ),
        patch("mcp_server.tools.memory._log_tool_usage", new=log),
        patch("mcp_server.tools.memory._touch_context_last_used", new=touch),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
        patch(
            "mcp_server.tools.memory._forgotten_by",
            new=AsyncMock(return_value=forgotten),
        ) as forgotten_by,
    ):
        yield SimpleNamespace(db=db, log=log, touch=touch, forgotten_by=forgotten_by)


# #2480: what ``forget_context_gate`` resolved, passed to every id's forget.
GATE = SimpleNamespace(name="the batch's context gate")


def _new_service():
    """A service stand-in whose context gate passes."""
    service = MagicMock()
    service.forget_context_gate = AsyncMock(return_value=GATE)
    return service


def _forgetter(outcomes: dict[UUID, object]):
    """A service whose forget answers per id: 1 / 0 deleted, or raises."""

    async def forget(request, **kwargs):
        outcome = outcomes[request.memory_id]
        if isinstance(outcome, BaseException):
            raise outcome
        ids = [request.memory_id] if outcome else []
        return SimpleNamespace(deleted_count=len(ids), memory_ids=ids)

    service = _new_service()
    service.forget = AsyncMock(side_effect=forget)
    return service


async def _call(args, service=None, context=None, forgotten=False, db_setup=None):
    service = service or _forgetter({})
    with _patched(service, context, forgotten) as mocks:
        if db_setup:
            db_setup(mocks.db)
        result = await handle_forget(
            {"context_id": str(uuid4()), **args}, user_id="u1", workspace_id=None
        )
    return json.loads(result[0].text), service, mocks


# ------------------------------------------------------------------ validation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [{"memory_id": str(uuid4())}, {"query": "old test data"}],
)
async def test_memory_ids_with_another_selector_is_refused(extra):
    body, service, _ = await _call({"memory_ids": [str(uuid4())], **extra})
    assert body["error"] == "invalid_argument"
    assert "exactly ONE" in body["message"]
    service.forget.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [[], "not-a-list", {"a": 1}])
async def test_memory_ids_must_be_a_non_empty_array(value):
    body, service, _ = await _call({"memory_ids": value})
    assert body["error"] == "invalid_argument"
    service.forget.assert_not_awaited()


@pytest.mark.asyncio
async def test_more_than_50_ids_is_refused():
    ids = [str(uuid4()) for _ in range(FORGET_MAX_IDS + 1)]
    body, service, _ = await _call({"memory_ids": ids})
    assert body["error"] == "invalid_argument"
    assert str(FORGET_MAX_IDS) in body["message"]
    service.forget.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_id_is_refused_and_named():
    dup = uuid4()
    body, service, _ = await _call({"memory_ids": [str(dup), str(uuid4()), str(dup).upper()]})
    assert body["error"] == "invalid_argument"
    assert str(dup) in body["message"]
    service.forget.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["nope", 42, None])
async def test_non_uuid_entry_is_refused_with_its_index(bad):
    body, service, _ = await _call({"memory_ids": [str(uuid4()), bad]})
    assert body["error"] == "invalid_argument"
    assert "memory_ids[1]" in body["message"]
    service.forget.assert_not_awaited()


# ------------------------------------------------------------------- outcomes


@pytest.mark.asyncio
async def test_mixed_outcomes_report_one_row_per_id_in_order():
    a, b, c = uuid4(), uuid4(), uuid4()
    service = _forgetter({a: 1, b: 0, c: AuthorizationError("no")})
    ctx = _context()
    body, service, mocks = await _call(
        {"memory_ids": [str(a), str(b), str(c)]}, service=service, context=ctx
    )

    assert body["status"] == "partial"
    assert body["deleted_count"] == 1
    assert body["memory_ids"] == [str(a)]
    assert [r["memory_id"] for r in body["results"]] == [str(a), str(b), str(c)]
    assert [r["status"] for r in body["results"]] == ["deleted", "skipped", "error"]
    assert body["results"][2]["error"] == "permission_denied"
    assert "message" in body["results"][2]
    assert "error" not in body["results"][0] and "error" not in body["results"][1]
    assert (body["count"], body["succeeded"], body["skipped"], body["failed"]) == (3, 1, 1, 1)
    assert body["context_id"] == str(ctx.id)
    assert body["context_name"] == "ctx"

    # The failed id's uncommitted work is rolled back; the call still commits once.
    mocks.db.rollback.assert_awaited_once()
    mocks.db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_each_id_goes_through_the_single_forget_call():
    a, b = uuid4(), uuid4()
    body, service, _ = await _call({"memory_ids": [str(a), str(b)]}, _forgetter({a: 1, b: 1}))
    assert body["status"] == "success"
    assert body["deleted_count"] == 2
    calls = service.forget.await_args_list
    assert [c.args[0].memory_id for c in calls] == [a, b]
    for c in calls:
        assert c.args[0].query is None
        assert c.kwargs["user_id"] == "u1"
        assert "current_context_id" in c.kwargs and "current_workspace_id" in c.kwargs


@pytest.mark.asyncio
async def test_skipped_ids_alone_keep_success():
    a = uuid4()
    body, _, _ = await _call({"memory_ids": [str(a)]}, _forgetter({a: 0}))
    assert body["status"] == "success"
    assert body["deleted_count"] == 0
    assert body["results"] == [{"memory_id": str(a), "status": "skipped"}]


@pytest.mark.asyncio
async def test_server_failure_carries_a_correlation_id():
    a, b = uuid4(), uuid4()
    body, _, _ = await _call(
        {"memory_ids": [str(a), str(b)]}, _forgetter({a: RuntimeError("boom"), b: 1})
    )
    assert body["status"] == "partial"
    row = body["results"][0]
    assert row["status"] == "error"
    assert row["error"] == "internal_error"
    assert "boom" not in row["message"]
    assert row["correlation_id"]
    assert body["results"][1]["status"] == "deleted"


@pytest.mark.asyncio
async def test_usage_logged_and_context_touched_once_per_call():
    ids = [uuid4() for _ in range(5)]
    ctx = _context()
    _, _, mocks = await _call(
        {"memory_ids": [str(i) for i in ids]}, _forgetter(dict.fromkeys(ids, 1)), ctx
    )
    mocks.log.assert_awaited_once()
    assert mocks.log.await_args.args[2] == "forget"
    assert mocks.log.await_args.args[4] == 200
    mocks.touch.assert_awaited_once()
    assert mocks.touch.await_args.args[1].id == ctx.id


@pytest.mark.asyncio
async def test_every_id_failing_is_an_error_envelope_with_the_rows():
    a, b = uuid4(), uuid4()
    body, _, mocks = await _call(
        {"memory_ids": [str(a), str(b)]},
        _forgetter({a: RuntimeError("x"), b: RuntimeError("y")}),
    )
    assert body["status"] == "error"
    assert body["error"] == "batch_failed"
    assert body["failed"] == 2 and body["deleted_count"] == 0
    assert [r["status"] for r in body["results"]] == ["error", "error"]
    assert mocks.log.await_args.args[4] == 500


@pytest.mark.asyncio
async def test_every_id_refused_logs_the_refusal_class():
    a = uuid4()
    _, _, mocks = await _call({"memory_ids": [str(a)]}, _forgetter({a: AuthorizationError("no")}))
    assert mocks.log.await_args.args[4] == 403


@pytest.mark.asyncio
async def test_a_failure_after_the_commit_reports_the_id_deleted():
    """The service commits before its audit write; a timeout there left the row deleted."""
    a, b = uuid4(), uuid4()
    body, _, mocks = await _call(
        {"memory_ids": [str(a), str(b)]},
        _forgetter({a: TimeoutError(), b: 1}),
        forgotten=True,
    )
    assert body["status"] == "success"
    assert [r["status"] for r in body["results"]] == ["deleted", "deleted"]
    assert body["memory_ids"] == [str(a), str(b)]
    assert mocks.forgotten_by.await_args.args[1:3] == (a, "u1")


@pytest.mark.asyncio
async def test_a_failed_rollback_stops_the_loop_and_keeps_the_rows():
    a, b, c = uuid4(), uuid4(), uuid4()

    def broken_rollback(db):
        db.rollback.side_effect = RuntimeError("connection lost")

    body, service, mocks = await _call(
        {"memory_ids": [str(a), str(b), str(c)]},
        _forgetter({a: 1, b: RuntimeError("boom"), c: 1}),
        db_setup=broken_rollback,
    )
    assert body["status"] == "partial"
    assert [r["status"] for r in body["results"]] == ["deleted", "error", "error"]
    assert body["results"][2]["error"] == "not_attempted"
    assert "session" in body["results"][2]["message"]
    assert body["memory_ids"] == [str(a)]
    assert service.forget.await_count == 2
    # The session is unusable: no usage row, touch or commit on it.
    mocks.log.assert_not_awaited()
    mocks.db.commit.assert_not_awaited()


# --------------------------------------------------------------- time budget
# #2191: the whole batch gets one deadline — the ``forget`` timeout — so a
# call never outlasts one tool timeout.


def _clock(*readings):
    return patch("mcp_server.tools.memory._monotonic", new=MagicMock(side_effect=list(readings)))


def _budget(seconds):
    return patch("mcp_server.tools.memory.get_tool_timeout", new=MagicMock(return_value=seconds))


@pytest.mark.asyncio
async def test_budget_spent_marks_the_remaining_ids_not_attempted():
    a, b, c = uuid4(), uuid4(), uuid4()
    with _budget(15), _clock(0, 1, 20):
        body, service, mocks = await _call(
            {"memory_ids": [str(a), str(b), str(c)]}, _forgetter({a: 1, b: 1, c: 1})
        )
    assert [r["status"] for r in body["results"]] == ["deleted", "error", "error"]
    for row in body["results"][1:]:
        assert row["error"] == "not_attempted"
        assert "time budget" in row["message"]
    assert service.forget.await_count == 1
    assert body["status"] == "partial"
    assert body["memory_ids"] == [str(a)]
    # The session is still good: one usage row and the commit, unlike a failed rollback.
    assert mocks.log.await_count == 1
    mocks.db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_id_is_not_started_on_a_sliver_of_the_budget():
    """Too little left to finish: untried, rather than cancelled mid-forget."""
    a, b = uuid4(), uuid4()
    with _budget(15), _clock(0, 1, 14.5):
        body, service, _ = await _call({"memory_ids": [str(a), str(b)]}, _forgetter({a: 1, b: 1}))
    assert [r["status"] for r in body["results"]] == ["deleted", "error"]
    assert body["results"][1]["error"] == "not_attempted"
    assert service.forget.await_count == 1


@pytest.mark.asyncio
async def test_each_wait_is_bounded_by_the_remaining_budget():
    from mcp_server.tools import memory

    a = uuid4()
    recorded: list[dict] = []
    real = memory.execute_with_timeout

    async def recorder(coro, timeout=None, operation_name="tool"):
        recorded.append({"timeout": timeout, "operation_name": operation_name})
        return await real(coro, timeout=timeout, operation_name=operation_name)

    with (
        _budget(15),
        _clock(0, 10),
        patch("mcp_server.tools.memory.execute_with_timeout", new=recorder),
    ):
        body, _, _ = await _call({"memory_ids": [str(a)]}, _forgetter({a: 1}))
    assert body["status"] == "success"
    # The context gate (#2480), right after the deadline is set, then the id.
    assert [r["timeout"] for r in recorded] == [15, pytest.approx(5)]
    assert {r["operation_name"] for r in recorded} == {"forget"}


@pytest.mark.asyncio
async def test_the_budget_is_the_forget_timeout():
    from mcp_server.tools._constants import TOOL_TIMEOUTS

    a = uuid4()
    budget = MagicMock(return_value=15)
    with patch("mcp_server.tools.memory.get_tool_timeout", new=budget):
        await _call({"memory_ids": [str(a)]}, _forgetter({a: 1}))
    budget.assert_called_once_with("forget")
    assert "forget_many" not in TOOL_TIMEOUTS
    assert "forget_batch" not in TOOL_TIMEOUTS


@pytest.mark.asyncio
@pytest.mark.parametrize("forgotten", [False, True])
async def test_a_stalled_id_at_the_deadline_is_a_timeout_row_then_not_attempted(forgotten):
    a, b, c = uuid4(), uuid4(), uuid4()
    with _budget(15), _clock(0, 0, 16):
        body, service, mocks = await _call(
            {"memory_ids": [str(a), str(b), str(c)]},
            _forgetter({a: TimeoutError(), b: 1, c: 1}),
            forgotten=forgotten,
        )
    rows = body["results"]
    assert [r["error"] for r in rows[1:]] == ["not_attempted", "not_attempted"]
    assert service.forget.await_count == 1
    mocks.db.rollback.assert_awaited_once()
    assert mocks.forgotten_by.await_args.args[1:3] == (a, "u1")
    if forgotten:
        assert rows[0] == {"memory_id": str(a), "status": "deleted"}
        assert body["status"] == "partial"
    else:
        assert rows[0]["status"] == "error"
        assert rows[0]["error"] == "timeout"
        assert rows[0]["correlation_id"]
        assert body["error"] == "batch_failed"
        assert body["failed"] == 3


@pytest.mark.asyncio
async def test_a_stalled_delete_ends_within_one_budget():
    """The issue's acceptance: a stall never holds the client past one budget."""
    ids = [uuid4() for _ in range(5)]
    never = asyncio.Event()

    async def stall(request, **kwargs):
        await never.wait()

    service = _new_service()
    service.forget = AsyncMock(side_effect=stall)
    with _budget(0.2):
        started = time.monotonic()
        body, service, _ = await _call({"memory_ids": [str(i) for i in ids]}, service)
        elapsed = time.monotonic() - started
    assert elapsed < 1.0
    rows = body["results"]
    assert rows[0]["error"] == "timeout"
    assert [r["error"] for r in rows[1:]] == ["not_attempted"] * 4
    assert service.forget.await_count == 1


@pytest.mark.asyncio
async def test_empty_memory_id_and_query_count_as_absent():
    a = uuid4()
    body, _, _ = await _call(
        {"memory_ids": [str(a)], "memory_id": "", "query": "", "k": 10}, _forgetter({a: 1})
    )
    assert body["status"] == "success"
    assert body["deleted_count"] == 1


@pytest.mark.asyncio
async def test_single_form_response_is_unchanged():
    mid = uuid4()
    body, service, _ = await _call({"memory_id": str(mid)}, _forgetter({mid: 1}))
    assert body["status"] == "success"
    assert body["deleted_count"] == 1
    assert body["memory_ids"] == [str(mid)]
    assert "results" not in body
    service.forget.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_batch_deletes_its_points_once_after_the_loop():
    """#2201: that step takes the global point-writer lock, so a batch queues
    its ids' points and takes it once, whatever the ids' outcomes."""
    a, b, c = uuid4(), uuid4(), uuid4()
    calls: list[str] = []

    async def forget(request, **kwargs):
        calls.append("forget")
        if request.memory_id == b:
            raise RuntimeError("boom")
        kwargs["point_queue"].append((request.memory_id, request.memory_id, "c"))
        return SimpleNamespace(deleted_count=1, memory_ids=[request.memory_id])

    async def drain(queued):
        calls.append("drain")
        assert [entry[0] for entry in queued] == [a, c]

    service = _new_service()
    service.forget = AsyncMock(side_effect=forget)
    service.delete_forgotten_points = AsyncMock(side_effect=drain)
    body, _, _ = await _call({"memory_ids": [str(a), str(b), str(c)]}, service)
    assert calls == ["forget", "forget", "forget", "drain"]
    assert body["status"] == "partial"


@pytest.mark.asyncio
async def test_nothing_queued_takes_no_lock():
    a = uuid4()
    service = _forgetter({a: 0})
    service.delete_forgotten_points = AsyncMock()
    await _call({"memory_ids": [str(a)]}, service)
    service.delete_forgotten_points.assert_not_awaited()


# ------------------------------------------------- #2480: one context gate


@pytest.mark.asyncio
async def test_the_batch_resolves_the_context_gate_once_and_passes_it_to_every_id():
    a, b, c = uuid4(), uuid4(), uuid4()
    service = _forgetter({a: 1, b: 0, c: 1})
    body, service, _ = await _call({"memory_ids": [str(a), str(b), str(c)]}, service)
    assert body["status"] == "success"
    service.forget_context_gate.assert_awaited_once()
    gate_call = service.forget_context_gate.await_args
    assert gate_call.args[0] == "u1"
    assert "key_workspace_id" in gate_call.kwargs
    assert [call.kwargs["context_gate"] for call in service.forget.await_args_list] == [GATE] * 3


@pytest.mark.asyncio
async def test_the_context_gate_runs_once_for_a_batch_of_real_forgets():
    """The issue's cost: ``_get_context_isolation_params`` ran once per id.
    A real ``MemoryService.forget`` per id, the gate counted on a fake."""
    from services.memory_service import MemoryService

    ws, ctx = uuid4(), uuid4()
    ids = [uuid4() for _ in range(5)]
    service = MemoryService(MagicMock(commit=AsyncMock(), rollback=AsyncMock()))
    service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
        return_value=(MagicMock(), str(ws), str(ctx))
    )
    # Nothing found: each id is ``skipped`` after the gate and its commit.
    service.memory_repo.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    with patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()):
        body, _, _ = await _call({"memory_ids": [str(i) for i in ids]}, service)
    assert [r["status"] for r in body["results"]] == ["skipped"] * 5
    assert service.memory_repo.get.await_count == 5
    assert service._get_context_isolation_params.await_count == 1


@pytest.mark.asyncio
async def test_a_refused_context_gate_fails_every_id_without_trying_one():
    """Every id would have been refused alike; none is tried."""
    a, b = uuid4(), uuid4()
    service = _forgetter({a: 1, b: 1})
    service.forget_context_gate = AsyncMock(side_effect=AuthorizationError("viewer"))
    body, service, mocks = await _call({"memory_ids": [str(a), str(b)]}, service)
    assert body["error"] == "batch_failed"
    assert [r["error"] for r in body["results"]] == ["permission_denied"] * 2
    assert [r["memory_id"] for r in body["results"]] == [str(a), str(b)]
    service.forget.assert_not_awaited()
    mocks.db.rollback.assert_awaited_once()
    assert mocks.log.await_args.args[4] == 403


@pytest.mark.asyncio
async def test_a_context_gate_that_outlasts_the_budget_leaves_every_id_not_attempted():
    """No id was tried, so none can have been deleted: ``not_attempted``
    (resend), not ``timeout`` (verify first, the delete may have landed)."""
    a, b = uuid4(), uuid4()
    never = asyncio.Event()

    async def stall(*args, **kwargs):
        await never.wait()

    service = _forgetter({a: 1, b: 1})
    service.forget_context_gate = AsyncMock(side_effect=stall)
    with _budget(0.1):
        body, service, mocks = await _call({"memory_ids": [str(a), str(b)]}, service)
    assert body["error"] == "batch_failed"
    assert [r["error"] for r in body["results"]] == ["not_attempted"] * 2
    assert all("checking the context" in r["message"] for r in body["results"])
    assert [r["memory_id"] for r in body["results"]] == [str(a), str(b)]
    service.forget.assert_not_awaited()
    assert mocks.log.await_args.args[4] == 500


# ----------------------------------- #2480: the single form's point delete


@pytest.mark.asyncio
async def test_the_single_form_deletes_the_points_after_the_budget():
    mid = uuid4()
    calls: list[str] = []

    async def forget(request, **kwargs):
        calls.append("forget")
        kwargs["point_queue"].append((mid, mid, "c"))
        return SimpleNamespace(deleted_count=1, memory_ids=[mid])

    async def drain(queued):
        calls.append("drain")
        assert queued == [(mid, mid, "c")]

    service = _new_service()
    service.forget = AsyncMock(side_effect=forget)
    service.delete_forgotten_points = AsyncMock(side_effect=drain)
    body, _, mocks = await _call({"query": "old notes"}, service)
    assert calls == ["forget", "drain"]
    assert body["status"] == "success" and body["deleted_count"] == 1
    mocks.db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_slow_point_delete_after_the_commit_does_not_fail_a_by_query_forget():
    """The issue's case: the by-query forget spent its budget before its
    commit, and the point delete after it stalls. That delete has its own
    bound (``FORGET_POINT_DELETE_TIMEOUT_SECONDS``) outside the tool budget,
    so the committed rows are reported forgotten, not a timeout. A real
    ``MemoryService.forget``; the vector store never answers."""
    from services.memory_service import MemoryService

    ws, ctx = uuid4(), uuid4()
    ids = [uuid4(), uuid4()]
    rows = {
        mid: SimpleNamespace(
            id=mid, workspace_id=ws, context_id=ctx, is_tool_triggered=False, point_id=mid
        )
        for mid in ids
    }
    service = MemoryService(MagicMock(commit=AsyncMock(), rollback=AsyncMock()))
    service._get_context_isolation_params = AsyncMock(  # type: ignore[method-assign]
        return_value=(MagicMock(), str(ws), str(ctx))
    )
    service.recall = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            degraded=False, results=[SimpleNamespace(memory_id=m) for m in ids]
        )
    )
    service.memory_repo.get = AsyncMock(side_effect=rows.get)  # type: ignore[method-assign]
    service._soft_delete_memory = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda user_id, memory, *a: memory.point_id
    )
    never = asyncio.Event()

    async def stall(*args, **kwargs):
        await never.wait()

    @contextlib.asynccontextmanager
    async def lock_free(self, point_deletes):
        yield list(point_deletes)

    with (
        patch("mcp_server.tools._helpers.get_tool_timeout", new=MagicMock(return_value=0.2)),
        patch("services.memory_service.FORGET_POINT_DELETE_TIMEOUT_SECONDS", 0.6),
        patch.object(MemoryService, "_points_free_to_delete", lock_free),
        patch("services.deferred_point_deletes.queue_point_deletes", AsyncMock()),
        patch("services.memory_service.resolve_collection_name", AsyncMock(return_value="c")),
        patch("services.memory_access_event_writer.emit_memory_access_event", AsyncMock()),
        patch("db.qdrant.delete_points_from_qdrant", new=stall),
    ):
        started = time.monotonic()
        body, _, mocks = await _call({"query": "old notes", "k": 2}, service)
        elapsed = time.monotonic() - started

    assert body["status"] == "success"
    assert body["deleted_count"] == 2
    assert body["memory_ids"] == [str(m) for m in ids]
    # Past the tool budget, within the delete's own bound.
    assert 0.2 < elapsed < 5.0
    assert mocks.log.await_args.args[4] == 200
    mocks.db.commit.assert_awaited_once()
