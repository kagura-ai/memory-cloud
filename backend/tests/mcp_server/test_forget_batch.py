"""forget(memory_ids=[...]) — the single forget applied per id (#2127).

The handler calls ``MemoryService.forget`` once per id, exactly as the
single form does, and reports one row per id: ``deleted`` / ``skipped`` /
``error``. The service commits each id itself, so a failing id is rolled
back alone. One usage row and one context touch cover the whole call.
"""

from __future__ import annotations

import contextlib
import json
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


def _forgetter(outcomes: dict[UUID, object]):
    """A service whose forget answers per id: 1 / 0 deleted, or raises."""

    async def forget(request, **kwargs):
        outcome = outcomes[request.memory_id]
        if isinstance(outcome, BaseException):
            raise outcome
        ids = [request.memory_id] if outcome else []
        return SimpleNamespace(deleted_count=len(ids), memory_ids=ids)

    service = MagicMock()
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
    assert body["memory_ids"] == [str(a)]
    assert service.forget.await_count == 2
    # The session is unusable: no usage row, touch or commit on it.
    mocks.log.assert_not_awaited()
    mocks.db.commit.assert_not_awaited()


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
