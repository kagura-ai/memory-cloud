"""``tools/list`` honours the URL's tool profile on both transports (#1601).

The selection rules themselves are pinned in ``test_tool_profiles``. Here a real
``tools/list`` JSON-RPC request is driven through each half of the dual-era
server — the session-based Streamable HTTP handler and the stateless MCP
2026-07-28 handler — with and without a query string, against the real tool
registry, down to the error shape each transport answers with.

The profile is a *view*: the last section pins that ``tools/call`` never looks
at it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import mcp_server.transport as transport
from mcp_server.tools import get_tool_definitions
from mcp_server.tools._profiles import CORE_TOOLS
from mcp_server.transport import TOOLS_LIST_TTL_MS, handle_streamable_http_post, mcp_asgi_app
from mcp_server.transport_stateless import handle_stateless_post

MODERN = "2026-07-28"
PV_KEY = "io.modelcontextprotocol/protocolVersion"
CAPS_KEY = "io.modelcontextprotocol/clientCapabilities"


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
    def raw(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages[1:])

    @property
    def body(self) -> dict:
        return json.loads(self.raw)


def _receive_for(payload: dict):
    raw = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


def _legacy_request(method: str = "tools/list", **params) -> dict:
    return {"jsonrpc": "2.0", "id": 4, "method": method, "params": params}


def _modern_request(method: str = "tools/list", **params) -> dict:
    params["_meta"] = {PV_KEY: MODERN, CAPS_KEY: {}}
    return {"jsonrpc": "2.0", "id": 4, "method": method, "params": params}


def _modern_headers(body: dict) -> dict[bytes, bytes]:
    headers = {b"mcp-protocol-version": MODERN.encode(), b"mcp-method": body["method"].encode()}
    name = body["params"].get("name")
    if isinstance(name, str):
        headers[b"mcp-name"] = name.encode()
    return headers


async def _legacy(query: bytes | None, body: dict | None = None) -> _Recorder:
    """Drive the session-based handler; ``query=None`` leaves the scope key out."""
    scope: dict = {"type": "http", "method": "POST", "path": "/mcp/"}
    if query is not None:
        scope["query_string"] = query
    send = _Recorder()
    session = SimpleNamespace(session_id="sess-1", user_id="user-1", workspace_id=None)
    await handle_streamable_http_post(
        scope, _receive_for(body or _legacy_request()), send, session, {}
    )
    return send


async def _stateless(query: bytes | None, body: dict | None = None) -> _Recorder:
    """Drive the stateless handler; ``query=None`` omits the keyword entirely."""
    body = body or _modern_request()
    kwargs: dict = {} if query is None else {"query_string": query}
    send = _Recorder()
    await handle_stateless_post(
        send, body, _modern_headers(body), user_id="user-1", workspace_id=None, **kwargs
    )
    return send


def _listed(send: _Recorder) -> list[str]:
    return [tool["name"] for tool in send.body["result"]["tools"]]


DRIVERS = pytest.mark.parametrize("drive", [_legacy, _stateless], ids=["session", "stateless"])


# --------------------------------------------------------------------- default


@DRIVERS
@pytest.mark.asyncio
@pytest.mark.parametrize("query", [None, b"", b"profile=full", b"session_id=mcp-1"])
async def test_without_a_selection_the_full_list_is_unchanged(drive, query):
    send = await drive(query)

    assert send.status == 200
    assert send.body["result"]["tools"] == get_tool_definitions()


@pytest.mark.asyncio
async def test_session_default_response_is_byte_for_byte_todays():
    send = await _legacy(None)

    assert (
        send.raw
        == json.dumps(
            {"jsonrpc": "2.0", "id": 4, "result": {"tools": get_tool_definitions()}}
        ).encode()
    )
    assert send.raw == (await _legacy(b"profile=full")).raw


@pytest.mark.asyncio
async def test_stateless_default_response_is_byte_for_byte_todays():
    send = await _stateless(None)

    server_info = send.body["result"]["_meta"]
    assert (
        send.raw
        == json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "result": {
                    "resultType": "complete",
                    "tools": get_tool_definitions(),
                    "ttlMs": TOOLS_LIST_TTL_MS,
                    "cacheScope": "public",
                    "_meta": server_info,
                },
            }
        ).encode()
    )
    assert send.raw == (await _stateless(b"profile=full")).raw


# ------------------------------------------------------------------- selection


@DRIVERS
@pytest.mark.asyncio
async def test_core_profile_lists_only_the_core_tools(drive):
    send = await drive(b"profile=core")

    assert send.status == 200
    assert _listed(send) == list(CORE_TOOLS)
    assert len(send.raw) < len((await drive(None)).raw) / 2


@DRIVERS
@pytest.mark.asyncio
async def test_allowlist_lists_the_named_tools_in_registry_order(drive):
    send = await drive(b"tools=reference,recall,remember&profile=core")

    assert _listed(send) == ["remember", "recall", "reference"]


@DRIVERS
@pytest.mark.asyncio
async def test_unknown_names_in_an_allowlist_are_ignored(drive):
    send = await drive(b"tools=recall,no_such_tool")

    assert send.status == 200
    assert _listed(send) == ["recall"]


@pytest.mark.asyncio
async def test_session_result_keeps_its_shape_and_session_header():
    send = await _legacy(b"profile=core")

    assert send.headers[b"content-type"] == b"application/json"
    assert send.headers[b"mcp-session-id"] == b"sess-1"
    assert set(send.body["result"]) == {"tools"}


@pytest.mark.asyncio
async def test_stateless_result_keeps_its_caching_hints():
    """The list varies by URL, never by caller: still shareable, so "public"."""
    send = await _stateless(b"profile=core")

    result = send.body["result"]
    assert b"mcp-session-id" not in send.headers
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == TOOLS_LIST_TTL_MS
    assert result["cacheScope"] == "public"


# ---------------------------------------------------------------------- errors


@DRIVERS
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, mentions",
    [
        (b"profile=minimal", ["minimal", "full", "core"]),
        (b"tools=rememberr,recal", ["rememberr", "recal"]),
        (b"tools=", ["tools"]),
    ],
)
async def test_bad_selection_is_a_jsonrpc_invalid_params_error(drive, query, mentions):
    send = await drive(query)

    body = send.body
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 4
    assert "result" not in body
    assert body["error"]["code"] == -32602
    for text in mentions:
        assert text in body["error"]["message"]


@pytest.mark.asyncio
async def test_session_error_is_http_200_like_its_sibling_errors():
    """The legacy handler answers JSON-RPC errors with HTTP 200 and keeps the
    session header, as its -32601 path does."""
    send = await _legacy(b"profile=minimal")

    assert send.status == 200
    assert send.headers[b"content-type"] == b"application/json"
    assert send.headers[b"mcp-session-id"] == b"sess-1"


@pytest.mark.asyncio
async def test_stateless_error_is_http_400_like_its_sibling_invalid_params():
    send = await _stateless(b"profile=minimal")

    assert send.status == 400
    assert send.headers[b"content-type"] == b"application/json"
    assert b"mcp-session-id" not in send.headers


# ------------------------------------------------------------ through the app


class _Sessions:
    _sessions: dict = {}

    async def get_or_create_session(self, **_kwargs):
        return SimpleNamespace(session_id="sess-1", user_id="user-1", workspace_id=None)

    async def get_session(self, _session_id):
        return SimpleNamespace(session_id="sess-1", user_id="user-1", workspace_id=None)


@pytest.fixture
def asgi(monkeypatch):
    """``mcp_asgi_app`` with auth and the session manager stubbed."""

    async def fake_auth(**_kwargs):
        return "user-1", None, None

    async def fake_workspace(_user_id):
        return None

    monkeypatch.setattr(transport, "authenticate_mcp_request", fake_auth)
    monkeypatch.setattr(transport, "_get_user_workspace_id", fake_workspace)
    monkeypatch.setattr(transport, "get_session_manager", _Sessions)

    async def call(body: dict, headers: dict[bytes, bytes], query: bytes) -> _Recorder:
        send = _Recorder()
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/mcp/",
            "query_string": query,
            "headers": list(headers.items()),
        }
        await mcp_asgi_app(scope, _receive_for(body), send)
        return send

    return call


@pytest.mark.asyncio
async def test_app_threads_the_query_string_to_the_session_handler(asgi):
    send = await asgi(_legacy_request(), {b"mcp-session-id": b"sess-1"}, b"profile=core")

    assert _listed(send) == list(CORE_TOOLS)


@pytest.mark.asyncio
async def test_app_threads_the_query_string_to_the_stateless_handler(asgi):
    body = _modern_request()
    send = await asgi(body, _modern_headers(body), b"tools=recall,remember")

    assert _listed(send) == ["remember", "recall"]
    assert b"mcp-session-id" not in send.headers


@pytest.mark.asyncio
async def test_session_id_in_the_query_still_resolves_next_to_a_profile(asgi):
    """``session_id`` and ``profile`` share the query string without colliding."""
    send = await asgi(_legacy_request(), {}, b"session_id=sess-1&profile=core")

    assert _listed(send) == list(CORE_TOOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize("era", ["session", "session-header", "stateless"])
async def test_undecodable_query_bytes_reach_the_selector_on_both_eras(asgi, era):
    """The selector tolerates bytes that are not UTF-8, so nothing on the way to
    it may be stricter: without a session header the session era first reads
    the query string for ``session_id``, and used to raise there."""
    body = _modern_request() if era == "stateless" else _legacy_request()
    headers = {
        "session": {},
        "session-header": {b"mcp-session-id": b"sess-1"},
        "stateless": _modern_headers(body),
    }[era]

    send = await asgi(body, headers, b"tools=recall&junk=\xff\xfe")

    assert send.status == 200
    assert _listed(send) == ["recall"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route, kwargs",
    [
        ("mcp_workspace_handler", {"workspace_id": "ws-1", "path": ""}),
        ("mcp_handler", {"path": ""}),
    ],
)
async def test_http_routes_forward_the_query_string(monkeypatch, route, kwargs):
    """The profile rides on ``/mcp/w/{workspace_id}?profile=core``: the FastAPI
    routes rebuild the scope, and must not drop its ``query_string`` doing so."""
    from starlette.requests import Request

    import api.main as main

    seen: dict = {}

    async def capture(scope, _receive, _send):
        seen.update(scope)

    monkeypatch.setattr(main, "mcp_asgi_app", capture)

    async def receive():  # pragma: no cover - the capture never reads the body
        return {"type": "http.disconnect"}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp/w/ws-1",
            "query_string": b"profile=core",
            "headers": [],
        },
        receive,
        _Recorder(),
    )
    await getattr(main, route)(request, **kwargs)

    assert seen["path"] == "/mcp/"
    assert seen["query_string"] == b"profile=core"


# ------------------------------------------------- a view, not an authorization


@DRIVERS
@pytest.mark.asyncio
@pytest.mark.parametrize("query", [b"profile=core", b"tools=recall", b"profile=no-such-profile"])
async def test_tools_call_ignores_the_profile(monkeypatch, drive, query):
    """A tool outside the listed set stays callable — the existing role checks
    in the tool handlers are the only gate — and a bad profile value, which
    fails ``tools/list``, does not fail a call."""
    import mcp_server.tools as tools_mod

    seen: dict = {}

    async def fake_execute(**kwargs):
        seen.update(kwargs)
        return [SimpleNamespace(type="text", text="ok")]

    monkeypatch.setattr(tools_mod, "execute_tool_call", fake_execute)

    assert "get_usage" not in CORE_TOOLS
    request = _legacy_request if drive is _legacy else _modern_request
    send = await drive(query, request("tools/call", name="get_usage", arguments={}))

    assert send.status == 200
    assert send.body["result"]["content"] == [{"type": "text", "text": "ok"}]
    assert seen["tool_name"] == "get_usage"
