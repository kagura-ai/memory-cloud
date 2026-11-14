"""The public ``tools/call`` pipeline (#2675): allowlist, arguments, limits, telemetry."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from mcp.types import TextContent

from mcp_server.public import dispatch, limits
from mcp_server.public.definitions import PUBLIC_TOOLS
from mcp_server.public.publication import ServingEndpoint
from mcp_server.public.telemetry import PUBLIC_CALL_OUTCOMES, PublicCallRecord
from mcp_server.public.tools import PublicArgumentError, PublicToolResult
from mcp_server.tools._helpers import _dumps
from utils.exceptions import CapacityLockedError, QuotaExceededError


def _endpoint() -> ServingEndpoint:
    return ServingEndpoint(
        id=uuid4(),
        public_id="pmcp_" + "A" * 22,
        workspace_id=uuid4(),
        context_id=uuid4(),
        resource_pk=uuid4(),
        resource_id="catalog",
        public_name="Catalog",
        public_description=None,
        public_usage_guide=None,
        context_name="catalog",
        owner_user_id="owner-1",
    )


def _body(content: list) -> dict[str, Any]:
    return json.loads(content[0].text)


def _ok(**payload: Any) -> PublicToolResult:
    return PublicToolResult(
        [TextContent(type="text", text=_dumps({"status": "success", **payload}))],
        "success",
        result_count=1,
    )


@pytest.fixture
def recorded(monkeypatch) -> list[PublicCallRecord]:
    records: list[PublicCallRecord] = []
    monkeypatch.setattr(dispatch, "record_public_call", records.append)
    return records


@pytest.fixture
def bucket(monkeypatch) -> AsyncMock:
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(dispatch, "check_public_rate_limit", mock)
    return mock


@pytest.fixture
def handlers(monkeypatch) -> dict[str, AsyncMock]:
    fakes = {name: AsyncMock(return_value=_ok(tool=name)) for name in PUBLIC_TOOLS}
    monkeypatch.setattr(dispatch, "PUBLIC_HANDLERS", fakes)
    return fakes


async def _call(endpoint, name, arguments=None, *, view=PUBLIC_TOOLS, session_id=None):
    return await dispatch.call_public_tool(
        endpoint, view, name, arguments, era="session", session_id=session_id
    )


HIDDEN = [
    "remember",
    "forget",
    "update_context",
    "delete_context",
    "setup_resource",
    "secret_get",
    "describe_tools",
    "list_contexts",
    "bootstrap",
    "nonexistent",
    "Recall",
    "recall ",
]


@pytest.mark.parametrize("name", HIDDEN)
async def test_hidden_names_are_denied_before_anything(name, recorded, bucket, handlers) -> None:
    endpoint = _endpoint()
    registry_dispatch = MagicMock()
    with patch("mcp_server.tools.execute_tool_call", registry_dispatch):
        content = await _call(endpoint, name, {"context_id": "x", "summary": "s"})
    assert getattr(content, "is_error", False) is True
    assert _body(content) == {
        "status": "error",
        "error": "unknown_tool",
        "message": f"Unknown tool: {name}",
    }
    registry_dispatch.assert_not_called()
    bucket.assert_not_called()
    assert all(h.await_count == 0 for h in handlers.values())
    assert [r.outcome for r in recorded] == ["denied"]
    assert recorded[0].tool == name


async def test_long_and_non_string_names_are_cut(recorded, bucket, handlers) -> None:
    content = await _call(_endpoint(), "x" * 500)
    assert _body(content)["message"] == "Unknown tool: " + "x" * 64
    assert recorded[0].tool == "x" * 64
    content = await _call(_endpoint(), None)
    assert _body(content)["error"] == "unknown_tool"


async def test_a_name_narrowed_away_is_denied(recorded, bucket, handlers) -> None:
    content = await _call(_endpoint(), "reference", {"memory_id": "m"}, view=("recall",))
    assert _body(content)["error"] == "unknown_tool"
    handlers["reference"].assert_not_awaited()
    bucket.assert_not_called()


async def test_each_public_tool_dispatches_to_its_handler(recorded, bucket, handlers) -> None:
    endpoint = _endpoint()
    for name in PUBLIC_TOOLS:
        content = await _call(endpoint, name, {})
        assert _body(content) == {"status": "success", "tool": name}
        handlers[name].assert_awaited_once_with(endpoint, {})
    assert [r.outcome for r in recorded] == ["success"] * 5
    assert bucket.await_count == 5


async def test_arguments_must_be_an_object(recorded, bucket, handlers) -> None:
    content = await _call(_endpoint(), "recall", ["query"])
    assert _body(content)["error"] == "invalid_argument"
    assert recorded[0].outcome == "invalid_request"
    bucket.assert_not_called()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("recall", {"query": "q", "context_ids": ["a"]}),
        ("recall", {"query": "q", "workspace_id": "w"}),
        ("recall", {"query": "q", "filters": {"type": "x"}}),
        ("list_tags", {"q": "x"}),
        ("get_context_info", {"include_stats": True}),
        ("explore", {"memory_id": "m", "user_id": "u"}),
    ],
)
async def test_unknown_arguments_are_refused_before_the_handler(
    name, arguments, recorded, bucket, handlers
) -> None:
    content = await _call(_endpoint(), name, arguments)
    body = _body(content)
    assert body["error"] == "invalid_argument"
    assert body["message"].startswith("unknown arguments: ")
    assert "This tool accepts: " in body["message"]
    handlers[name].assert_not_awaited()
    bucket.assert_not_called()
    assert recorded[0].outcome == "invalid_request"


async def test_version_on_reference_names_historical_versions(recorded, bucket, handlers) -> None:
    content = await _call(_endpoint(), "reference", {"memory_id": "m", "version": 1})
    assert _body(content) == {
        "status": "error",
        "error": "invalid_argument",
        "message": "historical versions are not available on this endpoint",
    }
    handlers["reference"].assert_not_awaited()
    bucket.assert_not_called()


async def test_a_foreign_context_id_is_not_available(recorded, bucket, handlers) -> None:
    endpoint = _endpoint()
    for value in (str(uuid4()), "not-a-uuid", 7, None):
        content = await _call(endpoint, "recall", {"query": "q", "context_id": value})
        assert _body(content) == {
            "status": "error",
            "error": "context_not_available",
            "message": "This endpoint serves one published dataset; omit context_id.",
        }
    handlers["recall"].assert_not_awaited()
    assert {r.outcome for r in recorded} == {"denied"}
    # The bound id itself (any case) is accepted.
    content = await _call(
        endpoint, "recall", {"query": "q", "context_id": str(endpoint.context_id).upper()}
    )
    assert _body(content)["status"] == "success"


async def test_rate_limited_call_is_a_tool_result(recorded, bucket, handlers) -> None:
    bucket.return_value = 60
    content = await _call(_endpoint(), "recall", {"query": "q"}, session_id="mcp-1")
    body = _body(content)
    assert body["error"] == "rate_limit_exceeded"
    assert body["retry_after_seconds"] == 60
    handlers["recall"].assert_not_awaited()
    assert recorded[0].outcome == "rate_limited"
    assert bucket.await_args.kwargs == {"session_id": "mcp-1", "bucket": "call"}


async def test_redis_outage_refuses_the_call(recorded, bucket, handlers) -> None:
    bucket.side_effect = limits.PublicRateLimitUnavailable()
    search = AsyncMock()
    with patch("services.search_service.SearchService.hybrid_search", search):
        content = await _call(_endpoint(), "recall", {"query": "q"})
    assert getattr(content, "is_error", False) is True
    assert _body(content) == {
        "status": "error",
        "error": "temporarily_unavailable",
        "message": "The public endpoint is briefly unavailable. Try again shortly.",
        "retry_after_seconds": 60,
    }
    handlers["recall"].assert_not_awaited()
    search.assert_not_awaited()
    assert [r.outcome for r in recorded] == ["error"]


async def test_argument_errors_from_the_handler(recorded, bucket, handlers) -> None:
    handlers["recall"].side_effect = PublicArgumentError("k must be an integer between 1 and 20.")
    content = await _call(_endpoint(), "recall", {"query": "q", "k": 21})
    assert _body(content) == {
        "status": "error",
        "error": "invalid_argument",
        "message": "k must be an integer between 1 and 20.",
    }
    assert recorded[0].outcome == "invalid_request"


@pytest.mark.parametrize(
    "exc",
    [
        QuotaExceededError("Workspace Acme has used 999 of 1000 calls", quota_type="x"),
        CapacityLockedError(
            "Acme is over capacity: 999 memories",
            memory_count=999,
            memory_limit=10,
            over_memories=989,
            used_bytes=None,
            storage_limit_bytes=None,
            over_bytes=None,
            cleanup_url="/secret/cleanup",
        ),
        PermissionError("/srv/secret/path"),
    ],
)
async def test_workspace_refusals_are_not_forwarded(exc, recorded, bucket, handlers) -> None:
    handlers["recall"].side_effect = exc
    content = await _call(_endpoint(), "recall", {"query": "q"})
    text = content[0].text
    assert _body(content)["error"] == "temporarily_unavailable"
    assert "Acme" not in text and "999" not in text and "secret" not in text
    assert recorded[0].outcome == "error"


async def test_server_failures_never_echo_the_exception(recorded, bucket, handlers) -> None:
    handlers["explore"].side_effect = RuntimeError("password=hunter2 at db-host")
    content = await _call(_endpoint(), "explore", {"memory_id": "m"})
    body = _body(content)
    assert getattr(content, "is_error", False) is True
    assert body["error"] == "internal_error"
    assert "correlation_id" in body
    assert "hunter2" not in content[0].text and "db-host" not in content[0].text
    assert recorded[0].outcome == "error"


async def test_telemetry_is_recorded_once_with_the_identity(recorded, bucket, handlers) -> None:
    endpoint = _endpoint()
    handlers["reference"].return_value = PublicToolResult(
        _ok().content, "success", result_count=1, continuation=True
    )
    await dispatch.call_public_tool(
        endpoint,
        PUBLIC_TOOLS,
        "reference",
        {"memory_id": "m", "content_offset": 10},
        era="stateless",
        session_id=None,
        protocol_version="2026-07-28",
    )
    assert len(recorded) == 1
    record = recorded[0]
    assert record.endpoint_id == endpoint.id
    assert record.public_id == endpoint.public_id
    assert record.workspace_id == endpoint.workspace_id
    assert record.context_id == endpoint.context_id
    assert record.resource_pk == endpoint.resource_pk
    assert record.resource_id == "catalog"
    assert record.tool == "reference"
    assert record.outcome in PUBLIC_CALL_OUTCOMES
    assert record.era == "stateless"
    assert record.protocol_version == "2026-07-28"
    assert record.result_count == 1
    assert record.continuation is True
    assert record.duration_ms >= 0


async def test_degraded_outcome_is_recorded(recorded, bucket, handlers) -> None:
    handlers["recall"].return_value = PublicToolResult(_ok().content, "degraded", result_count=0)
    await _call(_endpoint(), "recall", {"query": "q"})
    assert recorded[0].outcome == "degraded"


async def test_telemetry_failure_never_breaks_the_call(bucket, handlers) -> None:
    from mcp_server.public import telemetry

    with patch.object(telemetry, "logger") as log:
        log.info.side_effect = RuntimeError("log sink down")
        content = await _call(_endpoint(), "recall", {"query": "q"})
    assert _body(content)["status"] == "success"
    log.warning.assert_called_once()
    assert log.warning.call_args.args[0] == "public_mcp_telemetry_failed"


# ----------------------------------------------------------------------------
# check_public_rate_limit
# ----------------------------------------------------------------------------


@pytest.fixture
def counters(monkeypatch) -> dict[str, int]:
    counts: dict[str, int] = {}

    async def incr(key: str, amount: int, ttl: int | None = None) -> int:
        assert ttl == 60
        counts[key] = counts.get(key, 0) + amount
        return counts[key]

    monkeypatch.setattr(limits, "incrby_counter", incr)
    return counts


@pytest.fixture
def small_limits(monkeypatch):
    from config.settings import get_settings

    monkeypatch.setattr(get_settings(), "mcp_public_rate_limit_per_minute", 3)
    monkeypatch.setattr(get_settings(), "mcp_public_session_rate_limit_per_minute", 2)


async def test_call_bucket_trips_after_the_endpoint_limit(counters, small_limits) -> None:
    endpoint = _endpoint()
    results = [
        await limits.check_public_rate_limit(endpoint, session_id=None, bucket="call")
        for _ in range(4)
    ]
    assert results == [None, None, None, 60]
    assert set(counters) == {f"public_mcp:{endpoint.id}:call:minute"}


async def test_session_bucket_trips_after_the_session_limit(counters, small_limits) -> None:
    endpoint = _endpoint()
    results = [
        await limits.check_public_rate_limit(endpoint, session_id="mcp-abc", bucket="call")
        for _ in range(3)
    ]
    assert results == [None, None, 60]
    assert f"public_mcp:{endpoint.id}:sess:mcp-abc:minute" in counters
    # Another session has its own bucket (the endpoint bucket is shared).
    assert await limits.check_public_rate_limit(endpoint, session_id="mcp-def", bucket="call") == 60


async def test_control_bucket_is_wider(counters, small_limits) -> None:
    endpoint = _endpoint()
    results = [
        await limits.check_public_rate_limit(endpoint, session_id=None, bucket="control")
        for _ in range(16)
    ]
    assert results == [None] * 15 + [60]
    assert set(counters) == {f"public_mcp:{endpoint.id}:ctl:minute"}


async def test_redis_error_fails_closed_for_calls_and_open_for_control(monkeypatch) -> None:
    from db.redis import RedisError

    monkeypatch.setattr(limits, "incrby_counter", AsyncMock(side_effect=RedisError("down")))
    warn = MagicMock()
    monkeypatch.setattr(limits.logger, "warning", warn)
    with pytest.raises(limits.PublicRateLimitUnavailable):
        await limits.check_public_rate_limit(_endpoint(), session_id=None, bucket="call")
    assert (
        await limits.check_public_rate_limit(_endpoint(), session_id=None, bucket="control") is None
    )
    assert [c.args[0] for c in warn.call_args_list] == [
        "public_mcp_rate_limit_unavailable",
        "public_mcp_rate_limit_unavailable",
    ]


def test_keys_carry_no_client_identifier() -> None:
    import inspect

    source = inspect.getsource(limits).lower()
    for word in ("client_ip", "remote_addr", "x-forwarded-for", "user-agent", "user_agent"):
        assert word not in source


# ----------------------------------------------------------------------------
# #2805: the workspace-wide seven-day usage budget, charged before the handler
# ----------------------------------------------------------------------------

BUDGET_LIMIT = 25000


def _charge(admitted: bool = True, *, used: int = 1, retry: int | None = None):
    from datetime import datetime

    from services.public_usage_budget import BudgetCharge

    return BudgetCharge(
        admitted=admitted,
        calls_used=used,
        period_started_at=datetime(2026, 11, 10, 8, 0, 0),
        period_seq=1,
        retry_after_seconds=retry,
    )


@pytest.fixture
def order() -> list[str]:
    return []


@pytest.fixture
def budget(monkeypatch, order) -> AsyncMock:
    """Switch on (limit 25000); the charge admits and records its place."""

    async def charge(workspace_id, **kwargs):
        order.append("charge")
        return _charge()

    mock = AsyncMock(side_effect=charge)
    monkeypatch.setattr(dispatch, "public_usage_budget_limit", lambda: BUDGET_LIMIT)
    monkeypatch.setattr(dispatch, "charge_public_usage", mock)
    return mock


@pytest.fixture
def ordered(bucket, handlers, order):
    async def tick(*args, **kwargs):
        order.append("bucket")

    bucket.side_effect = tick
    for name, handler in handlers.items():

        async def run(endpoint, arguments, _name=name):
            order.append("handler")
            return _ok(tool=_name)

        handler.side_effect = run
    return order


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("remember", {"summary": "s"}),
        ("recall", ["not", "an", "object"]),
        ("recall", {"query": "q", "nope": 1}),
    ],
)
async def test_budget_refusals_before_the_bucket_never_charge(
    name, arguments, recorded, bucket, handlers, budget
) -> None:
    await _call(_endpoint(), name, arguments)
    bucket.assert_not_called()
    budget.assert_not_awaited()


async def test_minute_refusal_and_unreadable_redis_never_charge(
    recorded, bucket, handlers, budget
) -> None:
    bucket.return_value = 60
    content = await _call(_endpoint(), "recall", {"query": "q"})
    assert _body(content)["error"] == "rate_limit_exceeded"
    bucket.side_effect = limits.PublicRateLimitUnavailable()
    content = await _call(_endpoint(), "recall", {"query": "q"})
    assert _body(content)["error"] == "temporarily_unavailable"
    budget.assert_not_awaited()
    assert all(h.await_count == 0 for h in handlers.values())


async def test_wrong_context_id_never_charges(recorded, bucket, handlers, budget) -> None:
    content = await _call(_endpoint(), "recall", {"query": "q", "context_id": str(uuid4())})
    assert _body(content)["error"] == "context_not_available"
    budget.assert_not_awaited()


async def test_admitted_call_runs_the_handler_after_the_charge(
    recorded, ordered, budget, handlers
) -> None:
    endpoint = _endpoint()
    content = await _call(
        endpoint, "recall", {"query": "q", "context_id": str(endpoint.context_id)}
    )
    assert _body(content)["status"] == "success"
    assert ordered == ["bucket", "charge", "handler"]
    handlers["recall"].assert_awaited_once()
    assert recorded[0].outcome == "success"


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("provider down"),
        TimeoutError(),
        PublicArgumentError("bad value"),
        QuotaExceededError("over"),
    ],
    ids=["error", "timeout", "argument", "masked"],
)
async def test_a_failed_handler_stays_counted(failure, recorded, bucket, handlers, budget) -> None:
    handlers["recall"].side_effect = failure
    content = await _call(_endpoint(), "recall", {"query": "q"})
    assert getattr(content, "is_error", False) is True
    budget.assert_awaited_once()
    handlers["recall"].assert_awaited_once()


async def test_a_cancelled_call_stays_counted(recorded, bucket, handlers, budget) -> None:
    import asyncio

    handlers["recall"].side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await _call(_endpoint(), "recall", {"query": "q"})
    budget.assert_awaited_once()
    assert [r.outcome for r in recorded] == ["cancelled"]


async def test_exhausted_budget_refuses_without_the_handler(
    recorded, bucket, handlers, budget
) -> None:
    budget.side_effect = None
    budget.return_value = _charge(False, used=BUDGET_LIMIT, retry=3600)
    content = await _call(_endpoint(), "recall", {"query": "q"})
    assert getattr(content, "is_error", False) is True
    body = _body(content)
    assert body == {
        "status": "error",
        "error": "usage_budget_exhausted",
        "message": (
            "This public endpoint has used its call allowance for the current period. "
            "Try again later."
        ),
        "retry_after_seconds": 3600,
    }
    assert body["retry_after_seconds"] <= 3600
    assert str(BUDGET_LIMIT) not in content[0].text
    assert all(h.await_count == 0 for h in handlers.values())
    assert [(r.outcome, r.error_code) for r in recorded] == [
        ("rate_limited", "usage_budget_exhausted")
    ]


async def test_store_failure_refuses_as_unavailable(recorded, bucket, handlers, budget) -> None:
    from services.public_usage_budget import PublicUsageBudgetUnavailable

    budget.side_effect = PublicUsageBudgetUnavailable("down")
    content = await _call(_endpoint(), "recall", {"query": "q"})
    assert _body(content) == {
        "status": "error",
        "error": "temporarily_unavailable",
        "message": dispatch.UNAVAILABLE_MESSAGE,
        "retry_after_seconds": 60,
    }
    assert all(h.await_count == 0 for h in handlers.values())
    assert [(r.outcome, r.error_code) for r in recorded] == [("error", "usage_budget_unavailable")]


async def test_unexpected_charge_error_never_runs_the_handler(
    recorded, bucket, handlers, budget
) -> None:
    budget.side_effect = RuntimeError("bug")
    content = await _call(_endpoint(), "recall", {"query": "q"})
    assert _body(content)["error"] == "temporarily_unavailable"
    assert all(h.await_count == 0 for h in handlers.values())
    assert recorded[0].outcome == "error"


async def test_two_identical_calls_are_two_charges(recorded, bucket, handlers, budget) -> None:
    endpoint = _endpoint()
    for _ in range(2):
        await _call(endpoint, "recall", {"query": "q"}, session_id="mcp-same")
    assert budget.await_count == 2
    assert handlers["recall"].await_count == 2


async def test_switch_off_never_charges(monkeypatch, recorded, bucket, handlers) -> None:
    charge = AsyncMock(side_effect=AssertionError("charged"))
    monkeypatch.setattr(dispatch, "charge_public_usage", charge)
    monkeypatch.setattr(dispatch, "public_usage_budget_limit", lambda: None)
    content = await _call(_endpoint(), "recall", {"query": "q"})
    assert _body(content)["status"] == "success"
    charge.assert_not_awaited()


async def test_the_switch_is_read_once_per_call(monkeypatch, recorded, bucket, handlers) -> None:
    reads: list[int] = []

    def limit() -> int:
        reads.append(1)
        return BUDGET_LIMIT

    monkeypatch.setattr(dispatch, "public_usage_budget_limit", limit)
    monkeypatch.setattr(dispatch, "charge_public_usage", AsyncMock(return_value=_charge()))
    await _call(_endpoint(), "recall", {"query": "q"})
    assert reads == [1]


async def test_charge_is_keyed_by_the_loaded_workspace_only(
    recorded, bucket, handlers, budget
) -> None:
    endpoint = _endpoint()
    await _call(endpoint, "recall", {"query": "q"}, session_id="mcp-xyz")
    budget.assert_awaited_once_with(endpoint.workspace_id, limit=BUDGET_LIMIT)
