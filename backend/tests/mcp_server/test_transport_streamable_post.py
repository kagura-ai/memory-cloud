"""``handle_streamable_http_post`` method dispatch (#1541).

The handler explicitly served only ``initialize`` / ``tools/list`` /
``tools/call``; every other id-bearing request fell through to a "legacy
transport" path that dereferenced ``session.transport`` — an attribute
``MCPSession`` has not had since #248 — so any unknown method was a
guaranteed ``AttributeError`` → HTTP 500. ChatGPT started opening sessions
with ``server/discover`` (MCP 2026-07-28) and hit it on every registration.

These tests drive the handler directly with a fake session and a recording
ASGI ``send``. The fake session deliberately has NO ``transport`` attribute.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest

from mcp_server.transport import handle_streamable_http_post
from utils.exceptions import AuthorizationError


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int:
        return self.messages[0]["status"]

    @property
    def headers(self) -> dict[bytes, bytes]:
        return dict(self.messages[0].get("headers", []))

    @property
    def body(self) -> dict:
        raw = b"".join(m.get("body", b"") for m in self.messages[1:])
        return json.loads(raw)


def _receive_for(payload: dict):
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


def _session() -> SimpleNamespace:
    # No ``transport`` attribute — mirrors the real MCPSession dataclass.
    return SimpleNamespace(session_id="sess-1", user_id="user-1", workspace_id=None)


async def _post(payload: dict, *, query_string: bytes = b"") -> _Recorder:
    send = _Recorder()
    await handle_streamable_http_post(
        {"type": "http", "method": "POST", "path": "/mcp", "query_string": query_string},
        _receive_for(payload),
        send,
        _session(),
        {},
    )
    return send


# ------------------------------------------------------------- unknown methods


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["resources/list", "resources/templates/list", "prompts/list", "completely/made-up"],
)
async def test_unknown_request_method_is_a_jsonrpc_method_not_found(method):
    send = await _post({"jsonrpc": "2.0", "id": 7, "method": method, "params": {}})

    # HTTP 200 + JSON-RPC error, like the tools/call error path: this handler
    # is the legacy (initialize-handshake) half of the dual-era server. The
    # 404 + -32601 shape is the *modern* contract and is only produced for
    # requests carrying modern ``_meta`` (``test_transport_stateless``).
    assert send.status == 200
    assert send.headers[b"content-type"] == b"application/json"
    body = send.body
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    assert body["error"]["code"] == -32601
    assert method in body["error"]["message"]
    assert "result" not in body


@pytest.mark.asyncio
async def test_unknown_method_with_string_id_echoes_the_id():
    send = await _post({"jsonrpc": "2.0", "id": "discover-1", "method": "nope"})
    assert send.status == 200
    assert send.body["id"] == "discover-1"
    assert send.body["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_unknown_method_echo_is_length_capped():
    """The method name is client-supplied and unbounded — cap what we echo/log."""
    send = await _post({"jsonrpc": "2.0", "id": 1, "method": "x" * 5000})
    assert send.body["error"]["code"] == -32601
    assert len(send.body["error"]["message"]) < 200


# ------------------------------------------------------------------------ ping


@pytest.mark.asyncio
async def test_ping_returns_an_empty_result():
    send = await _post({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert send.status == 200
    assert send.body == {"jsonrpc": "2.0", "id": 3, "result": {}}


# -------------------------------------------------------------- server/discover


@pytest.mark.asyncio
async def test_server_discover_returns_a_complete_cacheable_discover_result():
    send = await _post(
        {
            "jsonrpc": "2.0",
            "id": "discover-1",
            "method": "server/discover",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientInfo": {"name": "ChatGPT", "version": "x"},
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        }
    )

    assert send.status == 200
    assert send.headers[b"content-type"] == b"application/json"
    assert send.headers[b"mcp-session-id"] == b"sess-1"
    body = send.body
    assert body["id"] == "discover-1"
    result = body["result"]

    # DiscoverResult per MCP 2026-07-28 — caching hints are MUST on a
    # ``complete`` discover result.
    assert result["resultType"] == "complete"
    assert isinstance(result["ttlMs"], int) and result["ttlMs"] >= 0
    assert result["cacheScope"] in ("public", "private")

    # Dual-era (#1544): the modern revision served statelessly by
    # ``transport_stateless`` plus the legacy ones ``initialize`` negotiates.
    # Legacy-only was the bug — a DiscoverResult is itself a modern signal, so
    # it stopped dual-era fallback and left modern-only clients no version.
    versions = result["supportedVersions"]
    assert "2026-07-28" in versions
    assert "2024-11-05" in versions  # what initialize negotiates today

    assert result["capabilities"] == {"tools": {}}
    server_info = result["_meta"]["io.modelcontextprotocol/serverInfo"]
    assert server_info["name"] == "kagura-memory-cloud"
    assert server_info["version"]
    assert isinstance(result["instructions"], str) and result["instructions"]


@pytest.mark.asyncio
async def test_server_discover_matches_initialize_identity():
    """Both surfaces must describe the same server."""
    init = await _post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    disc = await _post({"jsonrpc": "2.0", "id": 2, "method": "server/discover"})

    init_result = init.body["result"]
    disc_result = disc.body["result"]
    assert disc_result["_meta"]["io.modelcontextprotocol/serverInfo"] == init_result["serverInfo"]
    assert disc_result["capabilities"] == init_result["capabilities"]
    assert init_result["protocolVersion"] in disc_result["supportedVersions"]


# ------------------------------------------------------------- regressions


@pytest.mark.asyncio
async def test_notification_is_still_accepted_with_202_before_dispatch():
    send = await _post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert send.status == 202
    assert send.messages[1]["body"] == b""


@pytest.mark.asyncio
async def test_initialize_still_negotiates_the_legacy_protocol():
    send = await _post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert send.status == 200
    assert send.headers[b"mcp-session-id"] == b"sess-1"
    assert send.body["result"]["protocolVersion"] == "2024-11-05"


# ------------------------------------------------ version negotiation (review)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "negotiated"),
    [
        ("2025-03-26", "2025-03-26"),  # advertised by discover → must be echoed
        ("2024-11-05", "2024-11-05"),
        ("2025-06-18", "2024-11-05"),  # not advertised → fall back to the default
        ("2026-07-28", "2024-11-05"),  # advertised, but modern: no handshake form
        (None, "2024-11-05"),
    ],
)
async def test_initialize_echoes_every_version_discover_advertises(requested, negotiated):
    """``server/discover`` and ``initialize`` must not contradict each other: a
    client that picks a *legacy* version out of ``supportedVersions`` gets it
    echoed back
    (spec: a server that supports the requested version MUST answer with it)."""
    params = {} if requested is None else {"protocolVersion": requested}
    send = await _post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    assert send.body["result"]["protocolVersion"] == negotiated


@pytest.mark.asyncio
async def test_every_advertised_legacy_version_is_negotiable():
    """Modern revisions are excluded: they have no handshake to negotiate in —
    a client selects one per request (``test_transport_stateless``)."""
    from mcp_server.transport import MODERN_PROTOCOL_VERSIONS

    disc = await _post({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
    legacy = [
        v for v in disc.body["result"]["supportedVersions"] if v not in MODERN_PROTOCOL_VERSIONS
    ]
    assert legacy
    for version in legacy:
        init = await _post(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {"protocolVersion": version},
            }
        )
        assert init.body["result"]["protocolVersion"] == version


@pytest.mark.asyncio
async def test_initialize_tolerates_non_object_params():
    send = await _post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": [1]})
    assert send.status == 200
    assert send.body["result"]["protocolVersion"] == "2024-11-05"


# --------------------------------------------------- invalid requests (review)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, 42, True, "initialize"])
async def test_scalar_json_body_is_an_invalid_request_not_a_500(payload):
    send = await _post(payload)
    assert send.status == 400
    assert send.body["error"]["code"] == -32600
    assert send.body["id"] is None


@pytest.mark.asyncio
async def test_batch_array_is_rejected_instead_of_silently_accepted():
    """A batch used to satisfy ``"id" not in body`` (list membership) and was
    202'd as a notification, leaving the client waiting forever."""
    send = await _post([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}])
    assert send.status == 400
    assert send.body["error"]["code"] == -32600
    assert "batch" in send.body["error"]["message"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [None, 5, "", ["tools/list"]])
async def test_request_without_a_string_method_is_an_invalid_request(method):
    payload: dict = {"jsonrpc": "2.0", "id": 9}
    if method is not None:
        payload["method"] = method
    send = await _post(payload)
    assert send.status == 400
    assert send.body["id"] == 9
    assert send.body["error"]["code"] == -32600


# ------------------------------------- tools/list & tools/call (gate2 qa-lead)
# Both success paths were moved onto ``_send_jsonrpc_result``; their only other
# coverage is DB-backed integration tests that the unit job does not run.


@pytest.mark.asyncio
async def test_tools_list_returns_the_tool_definitions(monkeypatch):
    # #1601: tools/list reads the registry through the tool-profile selector.
    import mcp_server.tools._profiles as profiles_mod

    monkeypatch.setattr(
        profiles_mod, "get_tool_definitions", lambda: [{"name": "recall", "inputSchema": {}}]
    )
    send = await _post({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})

    assert send.status == 200
    assert send.headers[b"content-type"] == b"application/json"
    assert send.headers[b"mcp-session-id"] == b"sess-1"
    assert send.body == {
        "jsonrpc": "2.0",
        "id": 4,
        "result": {"tools": [{"name": "recall", "inputSchema": {}}]},
    }


@pytest.mark.asyncio
async def test_tools_call_formats_the_tool_result_and_passes_session_identity(monkeypatch):
    import mcp_server.tools as tools_mod

    seen: dict = {}

    async def fake_execute(**kwargs):
        seen.update(kwargs)
        return [SimpleNamespace(type="text", text='{"status":"success"}')]

    monkeypatch.setattr(tools_mod, "execute_tool_call", fake_execute)
    send = await _post(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "recall", "arguments": {"query": "x"}},
        }
    )

    assert send.status == 200
    assert send.headers[b"mcp-session-id"] == b"sess-1"
    # #1622: a success result carries no ``isError`` key at all ("if not set,
    # this is assumed to be false").
    assert send.body == {
        "jsonrpc": "2.0",
        "id": 5,
        "result": {"content": [{"type": "text", "text": '{"status":"success"}'}]},
    }
    assert seen == {
        "tool_name": "recall",
        "arguments": {"query": "x"},
        "user_id": "user-1",
        "workspace_id": None,
    }


# ------------------------------------------------------ isError (#1622)
# Tool *execution* errors are results, not JSON-RPC errors, and MCP marks
# them with ``CallToolResult.isError``. These drive the real
# ``execute_tool_call``: the pre-dispatch checks fail before any DB access.


@pytest.mark.asyncio
async def test_tools_call_error_envelope_sets_is_error_and_keeps_content():
    from mcp_server.tools._helpers import _error_response

    send = await _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "recall", "arguments": {"context_id": "not-a-uuid", "query": "x"}},
        }
    )

    assert send.status == 200
    body = send.body
    assert "error" not in body  # still a result, not a protocol error
    result = body["result"]
    assert result["isError"] is True
    # ``content`` is byte-identical to what the helper produced before #1622:
    # clients that only parse the envelope keep working.
    envelope = json.loads(result["content"][0]["text"])
    assert envelope["status"] == "error"
    assert envelope["error"] == "invalid_context_id_format"
    expected = _error_response("invalid_context_id_format", envelope["message"])[0].text
    assert result["content"] == [{"type": "text", "text": expected}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"name": "no_such_tool", "arguments": {}}, "unknown_tool"),
        ({"name": "recall", "arguments": {"query": "x"}}, "context_id_required"),
    ],
)
async def test_every_dispatch_level_envelope_is_flagged(params, code):
    send = await _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": params})

    result = send.body["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["error"] == code


@pytest.mark.asyncio
async def test_handler_exception_caught_by_dispatch_is_flagged(monkeypatch):
    """``execute_tool_call``'s catch-all turns a raising handler into a result;
    that result must be flagged too, not only the ``_error_response`` ones."""
    import mcp_server.tools as tools_mod

    async def broken(args, user_id, workspace_id):
        raise PermissionError("no access to context")

    monkeypatch.setattr(
        tools_mod, "_TOOL_REGISTRY", {**tools_mod._build_registry(), "recall": broken}
    )
    send = await _post(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "recall", "arguments": {"context_id": str(uuid4()), "query": "x"}},
        }
    )

    result = send.body["result"]
    assert result["isError"] is True
    envelope = json.loads(result["content"][0]["text"])
    # #1684: a stable code + help, not ``{"error": str(e)}``.
    assert envelope["error"] == "permission_denied"
    assert envelope["help"]
    assert "no access to context" not in result["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "code", "error_code"),
    [
        (ValueError("bad arg"), -32602, "validation_error"),
        # The code follows the classification, not the Python type (#1684):
        # a ValueError subclass is a server failure, an httpx timeout a timeout.
        (json.JSONDecodeError("Expecting value", "/srv/app/x.json", 0), -32603, "internal_error"),
        (httpx.ReadTimeout("read timed out"), -32001, "timeout"),
        (PermissionError("nope"), -32002, "permission_denied"),
        (AuthorizationError(), -32002, "permission_denied"),
        (TimeoutError(), -32001, "timeout"),
        (RuntimeError("x"), -32603, "internal_error"),
    ],
)
async def test_tools_call_failure_maps_to_a_jsonrpc_error(monkeypatch, exc, code, error_code):
    """The numeric codes are unchanged; ``message`` / ``data`` come from the
    #1684 vocabulary instead of the exception's text and type."""
    import mcp_server.tools as tools_mod

    async def boom(**_kwargs):
        raise exc

    monkeypatch.setattr(tools_mod, "execute_tool_call", boom)
    send = await _post(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "list_contexts"}}
    )

    assert send.status == 200
    assert send.body["id"] == 6
    assert send.body["error"]["code"] == code
    data = send.body["error"]["data"]
    assert data["error"] == error_code
    assert data["help"]
    assert "exception_type" not in data
    assert "details" not in data


@pytest.mark.asyncio
async def test_tools_call_failure_keeps_exception_detail_in_the_log(monkeypatch):
    """#1684: the legacy fallback used to return ``Internal error: <str(e)>`` and
    ``data.details``. A DSN / path now reaches the log only, joined to the
    response by ``correlation_id``."""
    import mcp_server.tools as tools_mod

    exc = RuntimeError("postgresql://user:hunter2@db/prod at /srv/app/secret.py")

    async def boom(**_kwargs):
        raise exc

    monkeypatch.setattr(tools_mod, "execute_tool_call", boom)
    with patch("mcp_server.tools._errors.logger") as log:
        send = await _post(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "remember"},
            }
        )

    wire = json.dumps(send.body)
    assert "hunter2" not in wire
    assert "/srv/app" not in wire
    error = send.body["error"]
    assert error["code"] == -32603
    assert error["message"] == "remember failed because of an unexpected server error."
    assert error["data"]["retryable"] is False  # a write with an unknown outcome
    assert error["data"]["outcome"] == "unknown"
    event = log.error.call_args
    assert event.kwargs["exc_info"] is exc
    assert event.kwargs["correlation_id"] == error["data"]["correlation_id"]


@pytest.mark.asyncio
async def test_tools_call_failure_without_params_still_answers():
    """A body whose ``params`` is not an object fails before the tool name is
    known; the fallback still answers with the vocabulary, as a write."""
    send = await _post({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": [1]})

    error = send.body["error"]
    assert error["code"] == -32603
    assert error["message"] == "The tool call failed because of an unexpected server error."
    assert error["data"]["error"] == "internal_error"
    assert error["data"]["retryable"] is False


# ------------------------------- id-less malformed envelopes (Copilot review)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"jsonrpc": "2.0"},
        {"jsonrpc": "2.0", "method": 5},
        {"jsonrpc": "2.0", "method": ""},
        {"jsonrpc": "2.0", "method": None},
    ],
)
async def test_idless_envelope_without_a_string_method_is_not_a_notification(payload):
    """The 202 notification short-circuit ran before method validation, so a
    malformed envelope with no ``id`` was silently accepted. Only a *valid*
    notification (string method, no id) earns the 202."""
    send = await _post(payload)
    assert send.status == 400
    assert send.body["error"]["code"] == -32600
    assert send.body["id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["notifications/initialized", "notifications/cancelled", "x"])
async def test_valid_notification_still_gets_202_with_no_body(method):
    send = await _post({"jsonrpc": "2.0", "method": method})
    assert send.status == 202
    assert send.messages[1]["body"] == b""


# ------------------------------------------------ guardrail digest (#1621)
# ``initialize`` now carries ``InitializeResult.instructions`` (Codex reads it
# from the handshake). With nothing selected it is exactly the base text and
# the handshake opens no database session — these tests run with a
# ``SimpleNamespace`` session and no DB, so any ``get_db`` here would hang or
# trip the fail-open path instead of passing.


@pytest.fixture(autouse=True)
def _no_ambient_agent_scope():
    from auth.agent_scope import set_agent_scope

    set_agent_scope(None)
    yield
    set_agent_scope(None)


@pytest.fixture
def get_db_spy(monkeypatch):
    from unittest.mock import AsyncMock

    import db.base as db_base

    calls: list[int] = []

    async def fake_get_db():
        calls.append(1)
        yield AsyncMock()

    monkeypatch.setattr(db_base, "get_db", fake_get_db)
    return calls


@pytest.mark.asyncio
async def test_initialize_returns_the_base_instructions_without_touching_the_db(
    get_db_spy,
):
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE

    send = await _post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    assert send.status == 200
    assert send.body["result"]["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert get_db_spy == []


@pytest.mark.asyncio
async def test_server_discover_without_selection_is_db_free_on_the_legacy_path(
    get_db_spy,
):
    from mcp_server.transport import DISCOVER_TTL_MS, SERVER_INSTRUCTIONS_BASE

    send = await _post({"jsonrpc": "2.0", "id": 2, "method": "server/discover"})

    result = send.body["result"]
    assert result["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert result["cacheScope"] == "public"
    assert result["ttlMs"] == DISCOVER_TTL_MS
    assert get_db_spy == []


@pytest.mark.asyncio
async def test_server_discover_instructions_match_initialize():
    """Extends ``test_server_discover_matches_initialize_identity``: the two
    surfaces describe the same server, including its instructions."""
    init = await _post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    disc = await _post({"jsonrpc": "2.0", "id": 2, "method": "server/discover"})

    assert disc.body["result"]["instructions"] == init.body["result"]["instructions"]


@pytest.mark.asyncio
async def test_initialize_and_discover_carry_the_same_digest_for_a_selected_context(
    monkeypatch, get_db_spy
):
    import services.guardrail_digest as digest_mod
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE, TOOLS_LIST_TTL_MS
    from services.guardrail_digest import DigestEntries, DigestEntry, digest_header

    ctx = uuid4()
    entries = DigestEntries(
        context_id=ctx,
        entries=[
            DigestEntry(
                memory_id=str(uuid4()),
                summary="squash-merge only after headRefOid matches the pushed SHA",
                importance=0.9,
                authored_by_caller=True,
                source_type="manual",
            )
        ],
        total_available=1,
        truncated=False,
        tool_triggered_version="0123456789abcdef",
    )

    async def fake_fetch_entries(db, **kwargs):
        assert kwargs["context_id"] == ctx
        return entries

    monkeypatch.setattr(digest_mod, "fetch_entries", fake_fetch_entries)
    qs = f"guardrails={ctx}".encode()

    init = await _post(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        query_string=qs,
    )
    disc = await _post({"jsonrpc": "2.0", "id": 2, "method": "server/discover"}, query_string=qs)

    text = init.body["result"]["instructions"]
    assert text.startswith(SERVER_INSTRUCTIONS_BASE + "\n\n" + digest_header(ctx) + "\n- (")
    assert "headRefOid" in text
    assert disc.body["result"]["instructions"] == text
    assert disc.body["result"]["cacheScope"] == "private"
    assert disc.body["result"]["ttlMs"] == TOOLS_LIST_TTL_MS
    assert len(get_db_spy) == 2  # one session per handshake / discover


@pytest.mark.asyncio
async def test_initialize_with_guardrails_off_is_the_base_text_and_db_free(get_db_spy):
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE

    send = await _post(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        query_string=b"guardrails=off",
    )

    assert send.body["result"]["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert get_db_spy == []
