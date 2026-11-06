"""Public ``tools/call`` → usage events (#2678): one offer per call, outcomes, privacy.

``call_public_tool`` runs with patched handlers and buckets; a recording
writer stands in for the per-process usage event writer. The transport is
driven like ``test_public_transport.py`` for the control methods.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from mcp.types import TextContent

import services.public_mcp_usage_writer as writer_module
from config.settings import get_settings
from mcp_server.public import dispatch, limits, telemetry, tools
from mcp_server.public import transport as pt
from mcp_server.public.definitions import PUBLIC_TOOLS
from mcp_server.public.limits import PublicRateLimitUnavailable
from mcp_server.public.publication import ServingEndpoint
from mcp_server.public.telemetry import PUBLIC_CALL_OUTCOMES, PublicCallRecord
from mcp_server.public.tools import PublicToolResult
from mcp_server.tools._helpers import _dumps
from models.memory import Memory
from models.public_mcp_usage import PMU_OUTCOMES
from services.public_mcp_usage_events import UsageEventInput, doc_key
from services.public_mcp_usage_writer import PublicMcpUsageWriter

KEY = "k" * 40


def _endpoint() -> ServingEndpoint:
    return ServingEndpoint(
        id=uuid4(),
        public_id="pmcp_" + uuid4().hex[:22],
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


class RecordingWriter:
    def __init__(self) -> None:
        self.offers: list[UsageEventInput] = []

    def offer(self, event: UsageEventInput) -> None:
        self.offers.append(event)


@pytest.fixture
def writer(monkeypatch) -> RecordingWriter:
    w = RecordingWriter()
    monkeypatch.setattr(writer_module, "_writer", w)
    return w


@pytest.fixture
def bucket(monkeypatch) -> AsyncMock:
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(dispatch, "check_public_rate_limit", mock)
    return mock


def _ok(
    name: str, *, doc_ids: tuple[str, ...] = (), continuation: bool = False
) -> PublicToolResult:
    return PublicToolResult(
        [TextContent(type="text", text=_dumps({"status": "success", "tool": name}))],
        "success",
        result_count=len(doc_ids) or None,
        continuation=continuation,
        doc_ids=doc_ids,
    )


@pytest.fixture
def handlers(monkeypatch) -> dict[str, AsyncMock]:
    fakes = {name: AsyncMock(return_value=_ok(name)) for name in PUBLIC_TOOLS}
    monkeypatch.setattr(dispatch, "PUBLIC_HANDLERS", fakes)
    return fakes


async def _call(endpoint, name, arguments=None, **kw):
    return await dispatch.call_public_tool(
        endpoint,
        PUBLIC_TOOLS,
        name,
        arguments,
        era=kw.pop("era", "session"),
        session_id=kw.pop("session_id", "sess-1"),
        **kw,
    )


# ----------------------------------------------------------------------------
# One event per completed outer tools/call
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", PUBLIC_TOOLS)
async def test_one_offer_per_call_with_server_resolved_identity(
    name, writer, bucket, handlers
) -> None:
    endpoint = _endpoint()
    await _call(
        endpoint,
        name,
        {"query": "q", "workspace_id": str(uuid4())} if name == "recall" else {},
        protocol_version="2025-06-18",
    )
    if name == "recall":
        # ``workspace_id`` is not a public argument: refused, still one event.
        assert writer.offers[0].outcome == "invalid_request"
    assert len(writer.offers) == 1
    event = writer.offers[0]
    assert event.endpoint_id == endpoint.id
    assert event.workspace_id == endpoint.workspace_id
    assert event.context_id == endpoint.context_id
    assert event.resource_pk == endpoint.resource_pk
    assert event.resource_id == endpoint.resource_id
    assert event.tool == name
    assert event.era == "session"
    assert event.protocol_version == "2025-06-18"
    assert event.duration_ms >= 0
    assert isinstance(event.started_at, datetime)
    assert event.started_at.tzinfo is None


async def test_handler_calling_another_handler_is_still_one_event(
    writer, bucket, monkeypatch
) -> None:
    async def outer(endpoint, arguments):
        inner = await tools.public_get_context_info(endpoint, {})
        return PublicToolResult(inner.content, "success", result_count=1)

    fakes = {name: AsyncMock(return_value=_ok(name)) for name in PUBLIC_TOOLS}
    fakes["recall"] = AsyncMock(side_effect=outer)
    monkeypatch.setattr(dispatch, "PUBLIC_HANDLERS", fakes)
    await _call(_endpoint(), "recall", {"query": "q"})
    assert len(writer.offers) == 1


# ----------------------------------------------------------------------------
# Outcomes (HTTP is 200 for every one of them; the result says what happened)
# ----------------------------------------------------------------------------


def test_outcome_vocabulary_matches_the_stored_one() -> None:
    assert PUBLIC_CALL_OUTCOMES == PMU_OUTCOMES


async def test_success_and_degraded(writer, bucket, handlers) -> None:
    await _call(_endpoint(), "recall", {"query": "q"})
    handlers["recall"].return_value = PublicToolResult(
        [TextContent(type="text", text=_dumps({"status": "success", "degraded": True}))],
        "degraded",
        result_count=1,
    )
    await _call(_endpoint(), "recall", {"query": "q"})
    assert [(e.outcome, e.error_code) for e in writer.offers] == [
        ("success", None),
        ("degraded", None),
    ]


async def test_denied_hidden_tool_is_recorded_as_other(writer, bucket, handlers) -> None:
    await _call(_endpoint(), "remember", {"summary": "s"})
    event = writer.offers[0]
    assert (event.outcome, event.error_code) == ("denied", "unknown_tool")
    built = _build(event)
    assert built["tool"] == "other"
    assert "remember" not in json.dumps(built, default=str)


async def test_rate_limited(writer, bucket, handlers) -> None:
    bucket.return_value = 60
    await _call(_endpoint(), "recall", {"query": "q"})
    assert (writer.offers[0].outcome, writer.offers[0].error_code) == (
        "rate_limited",
        "rate_limit_exceeded",
    )
    assert writer.offers[0].result_count is None


async def test_invalid_request(writer, bucket, handlers) -> None:
    await _call(_endpoint(), "recall", {"query": "q", "profile": "full"})
    assert (writer.offers[0].outcome, writer.offers[0].error_code) == (
        "invalid_request",
        "invalid_argument",
    )


async def test_redis_unavailable_is_error_temporarily_unavailable(writer, bucket, handlers) -> None:
    bucket.side_effect = PublicRateLimitUnavailable()
    content = await _call(_endpoint(), "recall", {"query": "q"})
    assert json.loads(content[0].text)["error"] == "temporarily_unavailable"
    handlers["recall"].assert_not_awaited()
    assert len(writer.offers) == 1
    assert (writer.offers[0].outcome, writer.offers[0].error_code) == (
        "error",
        "temporarily_unavailable",
    )


async def test_handler_exception_is_error(writer, bucket, handlers) -> None:
    handlers["recall"].side_effect = RuntimeError("boom")
    await _call(_endpoint(), "recall", {"query": "q"})
    assert writer.offers[0].outcome == "error"
    assert writer.offers[0].error_code is not None
    assert writer.offers[0].result_count is None
    assert writer.offers[0].doc_ids == ()


async def test_timeout_is_error_timeout(writer, bucket, monkeypatch) -> None:
    async def slow(endpoint, arguments):
        await asyncio.sleep(5)

    fakes = {name: AsyncMock(return_value=_ok(name)) for name in PUBLIC_TOOLS}
    fakes["recall"] = AsyncMock(side_effect=slow)
    monkeypatch.setattr(dispatch, "PUBLIC_HANDLERS", fakes)
    real = dispatch.execute_with_timeout
    monkeypatch.setattr(
        dispatch,
        "execute_with_timeout",
        lambda coro, operation_name: real(coro, timeout=0.05, operation_name=operation_name),
    )
    content = await _call(_endpoint(), "recall", {"query": "q"})
    assert getattr(content, "is_error", False) is True
    event = writer.offers[0]
    assert (event.outcome, event.error_code) == ("error", "timeout")
    assert event.duration_ms >= 40


async def test_cancelled_is_recorded_then_reraised(writer, bucket, monkeypatch) -> None:
    started = asyncio.Event()

    async def hang(endpoint, arguments):
        started.set()
        await asyncio.sleep(30)

    fakes = {name: AsyncMock(return_value=_ok(name)) for name in PUBLIC_TOOLS}
    fakes["recall"] = AsyncMock(side_effect=hang)
    monkeypatch.setattr(dispatch, "PUBLIC_HANDLERS", fakes)
    task = asyncio.create_task(_call(_endpoint(), "recall", {"query": "q"}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(writer.offers) == 1
    event = writer.offers[0]
    assert event.outcome == "cancelled"
    assert event.error_code is None
    assert event.result_count is None
    assert event.doc_ids == ()


# ----------------------------------------------------------------------------
# Documents and continuations
# ----------------------------------------------------------------------------


def _build(event: UsageEventInput, enabled: bool = False) -> dict[str, Any]:
    from services.public_mcp_usage_events import build_usage_event_row

    return build_usage_event_row(event, key=KEY, session_details_enabled=enabled).row


async def test_doc_ids_become_deduplicated_documents(writer, bucket, handlers) -> None:
    handlers["recall"].return_value = _ok("recall", doc_ids=("d1", "d1", "d2", "d1", "d2"))
    endpoint = _endpoint()
    await _call(endpoint, "recall", {"query": "q"})
    row = _build(writer.offers[0])
    assert row["documents"] == [
        {"doc_key": doc_key(endpoint.resource_pk, "d1", KEY), "chunks": 3, "rank": 1},
        {"doc_key": doc_key(endpoint.resource_pk, "d2", KEY), "chunks": 2, "rank": 3},
    ]


async def test_reference_initial_and_continuation(writer, bucket, handlers) -> None:
    endpoint = _endpoint()
    handlers["reference"].return_value = _ok("reference", doc_ids=("d1",))
    await _call(endpoint, "reference", {"memory_id": str(uuid4())})
    handlers["reference"].return_value = _ok("reference", doc_ids=("d1",), continuation=True)
    await _call(endpoint, "reference", {"memory_id": str(uuid4()), "content_offset": 100})
    first, more = (_build(e) for e in writer.offers)
    assert (first["continuation"], more["continuation"]) == (False, True)
    assert first["documents"] == more["documents"]


@pytest.fixture
def fake_db(monkeypatch):
    db = MagicMock()
    db.queue = []

    async def execute(stmt, *args, **kwargs):
        rows = db.queue.pop(0) if db.queue else []
        return SimpleNamespace(
            scalars=lambda: iter(rows),
            scalar_one_or_none=lambda: rows[0] if rows else None,
            first=lambda: rows[0] if rows else None,
            all=lambda: list(rows),
        )

    db.execute = execute

    @contextlib.asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(tools, "_read_session", _session)
    monkeypatch.setattr(tools, "_recall_session", _session)
    return db


def _memory(doc_id: str | None, summary: str = "s") -> Memory:
    m = Memory(
        id=uuid4(),
        summary=summary,
        content="text",
        type="fact",
        tags=[],
        created_at=datetime(2026, 11, 1),
        updated_at=datetime(2026, 11, 1),
    )
    m.resource_doc_id = doc_id
    m.observed_at = None
    return m


async def test_real_handlers_expose_the_returned_rows_doc_ids(fake_db, monkeypatch) -> None:
    endpoint = _endpoint()
    rows = [_memory("d1"), _memory("d2"), _memory("d1"), _memory(None)]
    monkeypatch.setattr(
        "services.search_service.SearchService.hybrid_search",
        AsyncMock(return_value=[{"id": str(m.id), "score": 0.5} for m in rows]),
    )
    fake_db.queue.append(rows)
    recall = await tools.public_recall(endpoint, {"query": "q"})
    assert recall.doc_ids == ("d1", "d2", "d1")

    fake_db.queue.append([rows[1]])
    reference = await tools.public_reference(endpoint, {"memory_id": str(rows[1].id)})
    assert reference.doc_ids == ("d2",)

    seed = SimpleNamespace(id=uuid4(), resource_doc_id="seed-doc", summary="seed")
    fake_db.queue.append([seed])

    async def neighbours(db, ep, frontier, exclude, limit):
        return [tools._Neighbour(uuid4(), "rel-doc", "r", "co_activation")]

    monkeypatch.setattr(tools, "_neighbours", neighbours)
    explore = await tools.public_explore(endpoint, {"memory_id": str(seed.id)})
    assert explore.doc_ids == ("seed-doc", "rel-doc")

    tags = await tools.public_get_context_info(endpoint, {})
    assert tags.doc_ids == ()


# ----------------------------------------------------------------------------
# Session details and privacy
# ----------------------------------------------------------------------------


async def test_session_details_only_when_switched_on(writer, bucket, handlers) -> None:
    await _call(_endpoint(), "list_tags", {}, client_name="Example Client/2.0")
    event = writer.offers[0]
    assert event.session_id == "sess-1"
    assert event.client_name == "Example Client/2.0"
    off = _build(event, enabled=False)
    on = _build(event, enabled=True)
    assert off["session_key"] is None and off["client_family"] is None
    assert len(on["session_key"]) == 32
    assert on["client_family"] == "exampleclient2.0"
    for row in (off, on):
        dumped = json.dumps(row, default=str)
        assert "sess-1" not in dumped
        assert "Example Client" not in dumped


async def test_log_line_carries_no_transient_fields(writer, monkeypatch) -> None:
    log = MagicMock()
    monkeypatch.setattr(telemetry, "logger", log)
    endpoint = _endpoint()
    telemetry.record_public_call(
        PublicCallRecord(
            endpoint_id=endpoint.id,
            public_id=endpoint.public_id,
            workspace_id=endpoint.workspace_id,
            context_id=endpoint.context_id,
            resource_pk=endpoint.resource_pk,
            resource_id=endpoint.resource_id,
            tool="recall",
            outcome="success",
            duration_ms=3,
            era="session",
            protocol_version=None,
            result_count=1,
            continuation=False,
            doc_ids=("secret-doc",),
            session_id="raw-session",
            client_name="Some Client",
        )
    )
    fields = log.info.call_args.kwargs
    assert not {"doc_ids", "session_id", "client_name", "started_at"} & set(fields)
    assert len(writer.offers) == 1


async def test_client_name_is_kept_on_the_session_at_initialize(monkeypatch) -> None:
    from mcp_server.session import get_session_manager

    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)
    endpoint = _endpoint()
    monkeypatch.setattr(pt, "_load", AsyncMock(return_value=(endpoint, False)))
    monkeypatch.setattr(limits, "incrby_counter", AsyncMock(return_value=1))
    send = await _asgi(
        endpoint,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "Ex Client"}},
        },
    )
    session_id = dict(send.messages[0]["headers"])[b"mcp-session-id"].decode()
    session = await get_session_manager().get_session(session_id)
    assert session is not None
    assert session.client_name == "Ex Client"


# ----------------------------------------------------------------------------
# Control methods record nothing
# ----------------------------------------------------------------------------


class _Send:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)


async def _asgi(endpoint, payload, headers: dict[bytes, bytes] | None = None) -> _Send:
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/public/",
        "query_string": b"",
        "headers": list((headers or {}).items()),
        "public_endpoint_public_id": endpoint.public_id,
    }
    send = _Send()
    await pt.public_mcp_asgi_app(scope, receive, send)
    return send


async def test_control_methods_record_nothing(writer, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)
    endpoint = _endpoint()
    monkeypatch.setattr(pt, "_load", AsyncMock(return_value=(endpoint, False)))
    monkeypatch.setattr(limits, "incrby_counter", AsyncMock(return_value=1))
    send = await _asgi(
        endpoint,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        },
    )
    session = {b"mcp-session-id": dict(send.messages[0]["headers"])[b"mcp-session-id"]}
    for method in ("tools/list", "ping"):
        await _asgi(endpoint, {"jsonrpc": "2.0", "id": 2, "method": method}, session)
    modern = "2026-07-28"
    for method in ("server/discover", "tools/list", "ping"):
        await _asgi(
            endpoint,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": method,
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": modern,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
            {b"mcp-protocol-version": modern.encode(), b"mcp-method": method.encode()},
        )
    assert writer.offers == []


async def test_a_tools_call_through_the_transport_records_once(writer, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "mcp_public_endpoints_enabled", True)
    endpoint = _endpoint()
    monkeypatch.setattr(pt, "_load", AsyncMock(return_value=(endpoint, False)))
    monkeypatch.setattr(limits, "incrby_counter", AsyncMock(return_value=1))
    send = await _asgi(
        endpoint,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "Ex"}},
        },
    )
    session = {b"mcp-session-id": dict(send.messages[0]["headers"])[b"mcp-session-id"]}
    send = await _asgi(
        endpoint,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_context_info", "arguments": {}},
        },
        session,
    )
    assert send.messages[0]["status"] == 200
    assert len(writer.offers) == 1
    assert writer.offers[0].client_name == "Ex"
    assert writer.offers[0].outcome == "success"


# ----------------------------------------------------------------------------
# A dead or failing writer never changes a tool result
# ----------------------------------------------------------------------------


async def test_no_writer_running_changes_nothing(bucket, handlers, monkeypatch) -> None:
    monkeypatch.setattr(writer_module, "_writer", None)
    content = await _call(_endpoint(), "recall", {"query": "q"})
    assert json.loads(content[0].text) == {"status": "success", "tool": "recall"}


async def test_writer_raising_changes_nothing_and_logs_once(bucket, handlers, monkeypatch) -> None:
    failing = MagicMock()
    failing.offer.side_effect = RuntimeError("writer bug")
    monkeypatch.setattr(writer_module, "_writer", failing)
    log = MagicMock()
    monkeypatch.setattr(telemetry, "logger", log)
    content = await _call(_endpoint(), "recall", {"query": "q"})
    assert json.loads(content[0].text) == {"status": "success", "tool": "recall"}
    failed = [c for c in log.warning.call_args_list if c.args[0] == "public_mcp_telemetry_failed"]
    assert len(failed) == 1
    assert failed[0].kwargs == {"error_type": "RuntimeError"}


async def test_stopped_writer_does_not_block_the_call(bucket, handlers, monkeypatch) -> None:
    db_never = MagicMock(side_effect=AssertionError("the request path touched the database"))
    stopped = PublicMcpUsageWriter(
        session_factory=db_never,
        queue_size=1,
        flush_interval=1.0,
        write_timeout=1.0,
        key=KEY,
        session_details_enabled=False,
    )
    monkeypatch.setattr(writer_module, "_writer", stopped)
    for _ in range(3):  # one queued, two dropped as queue_full
        content = await _call(_endpoint(), "recall", {"query": "q"})
        assert json.loads(content[0].text) == {"status": "success", "tool": "recall"}
    assert stopped.health().dropped_since_start == 2
    db_never.assert_not_called()
