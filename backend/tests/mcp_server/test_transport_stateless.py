"""Dual-era MCP server: the stateless MCP 2026-07-28 path (#1544).

#1541 taught ``server/discover`` to answer, but the ``DiscoverResult`` listed
only legacy (initialize-handshake) revisions. A ``DiscoverResult`` is itself a
*modern* signal, so a dual-era client stops falling back to ``initialize`` and
a modern-only client has no version it can speak — registration ended right
after a successful discover.

The server is now dual-era: a request carrying modern per-request ``_meta`` is
served statelessly (no ``Mcp-Session-Id``, no ``initialize``), while
``initialize`` keeps selecting the legacy, session-scoped behaviour.

Two layers are covered:

* ``handle_stateless_post`` driven directly (validation order, error shapes,
  result envelopes);
* ``mcp_asgi_app`` with auth and the session manager stubbed, to pin the era
  split itself — above all that the modern path never touches a session and
  the legacy path still gets its (already-read) body.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

import mcp_server.transport as transport
from mcp_server.transport import (
    LEGACY_PROTOCOL_VERSIONS,
    MODERN_PROTOCOL_VERSIONS,
    SUPPORTED_PROTOCOL_VERSIONS,
    _is_modern_request,
    mcp_asgi_app,
)
from mcp_server.transport_stateless import handle_stateless_post

MODERN = "2026-07-28"
PV_KEY = "io.modelcontextprotocol/protocolVersion"
CAPS_KEY = "io.modelcontextprotocol/clientCapabilities"
INFO_KEY = "io.modelcontextprotocol/clientInfo"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"


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


def _meta(version: str = MODERN) -> dict:
    return {
        PV_KEY: version,
        INFO_KEY: {"name": "ExampleClient", "version": "1.0.0"},
        CAPS_KEY: {},
    }


def _request(method: str, params: dict | None = None, *, request_id=1, version=MODERN) -> dict:
    merged = dict(params or {})
    merged["_meta"] = _meta(version)
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": merged}


def _headers(body: dict, **overrides: str | None) -> dict[bytes, bytes]:
    """The request-metadata headers a conforming client mirrors from ``body``."""
    params = body.get("params") or {}
    values: dict[str, str | None] = {
        "mcp-protocol-version": (params.get("_meta") or {}).get(PV_KEY),
        "mcp-method": body.get("method"),
        "mcp-name": params.get("name"),
    }
    values.update(overrides)
    return {k.encode(): v.encode() for k, v in values.items() if isinstance(v, str)}


async def _post(body: dict, headers: dict[bytes, bytes] | None = None) -> _Recorder:
    send = _Recorder()
    await handle_stateless_post(
        send,
        body,
        _headers(body) if headers is None else headers,
        user_id="user-1",
        workspace_id=None,
    )
    return send


def _assert_stateless(send: _Recorder) -> None:
    """MCP 2026-07-28 has no protocol-level session: never mint or echo an id."""
    assert b"mcp-session-id" not in send.headers


# ------------------------------------------------------------ version constants


def test_supported_versions_are_the_modern_and_legacy_sets_combined():
    assert MODERN in MODERN_PROTOCOL_VERSIONS
    assert MODERN not in LEGACY_PROTOCOL_VERSIONS
    assert set(SUPPORTED_PROTOCOL_VERSIONS) == set(MODERN_PROTOCOL_VERSIONS) | set(
        LEGACY_PROTOCOL_VERSIONS
    )


# -------------------------------------------------------------- server/discover


@pytest.mark.asyncio
async def test_discover_advertises_the_modern_revision_statelessly():
    send = await _post(_request("server/discover", request_id="discover-1"))

    assert send.status == 200
    assert send.headers[b"content-type"] == b"application/json"
    _assert_stateless(send)
    result = send.body["result"]
    assert send.body["id"] == "discover-1"
    assert result["resultType"] == "complete"
    # The whole point of #1544: a modern client must find a version it speaks.
    assert MODERN in result["supportedVersions"]
    # ... and an initialize client still finds the legacy ones.
    assert set(LEGACY_PROTOCOL_VERSIONS) <= set(result["supportedVersions"])
    assert isinstance(result["ttlMs"], int) and result["ttlMs"] >= 0
    assert result["cacheScope"] == "public"
    assert result["capabilities"] == {"tools": {}}
    assert result["_meta"][SERVER_INFO_KEY]["name"] == "kagura-memory-cloud"


# ------------------------------------------------------------------------- ping


@pytest.mark.asyncio
async def test_ping_returns_a_complete_empty_result():
    send = await _post(_request("ping", request_id=3))

    assert send.status == 200
    _assert_stateless(send)
    result = send.body["result"]
    assert result["resultType"] == "complete"
    assert result["_meta"][SERVER_INFO_KEY]["name"] == "kagura-memory-cloud"
    assert set(result) == {"resultType", "_meta"}


# ------------------------------------------------------------------- tools/list


@pytest.mark.asyncio
async def test_tools_list_is_complete_cacheable_and_sessionless(monkeypatch):
    import mcp_server.tools as tools_mod

    monkeypatch.setattr(
        tools_mod, "get_tool_definitions", lambda: [{"name": "recall", "inputSchema": {}}]
    )
    send = await _post(_request("tools/list", request_id=4))

    assert send.status == 200
    _assert_stateless(send)
    result = send.body["result"]
    assert result["tools"] == [{"name": "recall", "inputSchema": {}}]
    assert result["resultType"] == "complete"
    # Caching hints are MUST on a complete tools/list result.
    assert isinstance(result["ttlMs"], int) and result["ttlMs"] >= 0
    assert result["cacheScope"] == "public"
    assert result["_meta"][SERVER_INFO_KEY]["version"]


# ------------------------------------------------------------------- tools/call


@pytest.mark.asyncio
async def test_tools_call_runs_with_the_authenticated_identity(monkeypatch):
    import mcp_server.tools as tools_mod

    seen: dict = {}

    async def fake_execute(**kwargs):
        seen.update(kwargs)
        return [SimpleNamespace(type="text", text='{"status":"success"}')]

    monkeypatch.setattr(tools_mod, "execute_tool_call", fake_execute)
    body = _request("tools/call", {"name": "recall", "arguments": {"query": "x"}}, request_id=5)
    send = _Recorder()
    await handle_stateless_post(send, body, _headers(body), user_id="user-9", workspace_id="ws-9")

    assert send.status == 200
    _assert_stateless(send)
    result = send.body["result"]
    assert result["resultType"] == "complete"
    assert result["content"] == [{"type": "text", "text": '{"status":"success"}'}]
    # Identity comes from the request's own credentials, never from a session;
    # ``_meta`` is protocol metadata and must not leak into the tool arguments.
    assert seen == {
        "tool_name": "recall",
        "arguments": {"query": "x"},
        "user_id": "user-9",
        "workspace_id": "ws-9",
    }


@pytest.mark.asyncio
async def test_tools_call_without_arguments_defaults_to_an_empty_object(monkeypatch):
    import mcp_server.tools as tools_mod

    seen: dict = {}

    async def fake_execute(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(tools_mod, "execute_tool_call", fake_execute)
    send = await _post(_request("tools/call", {"name": "list_contexts"}))

    assert send.status == 200
    assert seen["arguments"] == {}
    assert send.body["result"]["content"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (ValueError("bad arg"), -32602),
        (PermissionError("nope"), -32603),
        (TimeoutError(), -32603),
        (RuntimeError("secret dsn"), -32603),
    ],
)
async def test_tools_call_failure_uses_only_standard_jsonrpc_codes(monkeypatch, exc, code):
    """-32000..-32019 is the spec's *legacy* sub-range (SHOULD NOT be used by a
    2026-07-28 implementation), so the modern path does not reuse the legacy
    path's custom -32001 / -32002."""
    import mcp_server.tools as tools_mod

    async def boom(**_kwargs):
        raise exc

    monkeypatch.setattr(tools_mod, "execute_tool_call", boom)
    send = await _post(_request("tools/call", {"name": "recall"}, request_id=6))

    assert send.status == 200
    _assert_stateless(send)
    error = send.body["error"]
    assert send.body["id"] == 6
    assert error["code"] == code
    assert error["data"]["exception_type"] == type(exc).__name__


@pytest.mark.asyncio
async def test_unexpected_tool_exception_text_is_not_echoed_to_the_client(monkeypatch):
    import mcp_server.tools as tools_mod

    async def boom(**_kwargs):
        raise RuntimeError("postgresql://user:hunter2@db/prod")

    monkeypatch.setattr(tools_mod, "execute_tool_call", boom)
    send = await _post(_request("tools/call", {"name": "recall"}))

    assert "hunter2" not in json.dumps(send.body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"name": 5},
        {"name": ""},
        {},
        {"name": "recall", "arguments": [1, 2]},
    ],
)
async def test_tools_call_with_malformed_params_is_invalid_params(params):
    body = _request("tools/call", params)
    # Mirror a header so the failure is attributable to the body, not Mcp-Name.
    send = await _post(body, _headers(body, **{"mcp-name": "recall"}))

    assert send.status == 400
    assert send.body["error"]["code"] == -32602


# ---------------------------------------------------------------- unknown method


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["resources/list", "prompts/list", "subscriptions/listen"])
async def test_unknown_method_is_404_with_a_jsonrpc_method_not_found(method):
    """The modern contract: 404 **and** a -32601 body — the body is what tells a
    client this is a modern server and not a legacy one missing the endpoint.
    (The legacy path keeps 200 + -32601; see test_transport_streamable_post.)"""
    send = await _post(_request(method, request_id=7))

    assert send.status == 404
    _assert_stateless(send)
    assert send.body["id"] == 7
    assert send.body["error"]["code"] == -32601
    assert method in send.body["error"]["message"]


@pytest.mark.asyncio
async def test_unknown_method_echo_is_length_capped():
    send = await _post(_request("x" * 5000))
    assert send.status == 404
    assert len(send.body["error"]["message"]) < 200


# ------------------------------------------------- UnsupportedProtocolVersion


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", ["1900-01-01", "2027-01-01", "2025-03-26", "2024-11-05"])
async def test_unsupported_version_is_400_with_the_supported_list(requested):
    """A legacy revision in per-request ``_meta`` is unsupported too: those are
    only reachable through ``initialize``."""
    send = await _post(_request("tools/list", version=requested))

    assert send.status == 400
    _assert_stateless(send)
    error = send.body["error"]
    assert error["code"] == -32022
    assert error["data"]["requested"] == requested
    assert error["data"]["supported"] == list(SUPPORTED_PROTOCOL_VERSIONS)
    assert MODERN in error["data"]["supported"]


@pytest.mark.asyncio
async def test_unsupported_version_is_settled_before_every_other_rule():
    """The header / ``clientCapabilities`` rules belong to the revision we
    implement; a client on another revision must be told about the version (and
    what to retry with), not blamed for a rule its revision may not define."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {"_meta": {PV_KEY: "2027-01-01"}},  # no clientCapabilities
    }
    send = await _post(body, {})  # and no mirrored headers at all

    assert send.status == 400
    assert send.body["error"]["code"] == -32022
    assert MODERN in send.body["error"]["data"]["supported"]


# --------------------------------------------------------------- HeaderMismatch


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"mcp-protocol-version": "2025-03-26"},  # header != body _meta
        {"mcp-protocol-version": "2027-01-01"},
        {"mcp-method": "tools/call"},  # header != body method
        {"mcp-method": "Tools/List"},  # header *values* are case-sensitive
    ],
)
async def test_header_body_disagreement_is_a_400_header_mismatch(overrides):
    """A header that contradicts the body is what the validation rule is for:
    an intermediary would act on one value while we execute the other."""
    body = _request("tools/list", request_id=8)
    send = await _post(body, _headers(body, **overrides))

    assert send.status == 400
    _assert_stateless(send)
    assert send.body["id"] == 8
    assert send.body["error"]["code"] == -32020


@pytest.mark.asyncio
@pytest.mark.parametrize("name_header", ["remember", "=?base64?!!!not-base64!!!?=", "Recall"])
async def test_tools_call_name_header_must_match_the_body(monkeypatch, name_header):
    import mcp_server.tools as tools_mod

    async def must_not_run(**_kwargs):  # pragma: no cover - the assertion
        raise AssertionError("a request with mismatched headers reached the tool")

    monkeypatch.setattr(tools_mod, "execute_tool_call", must_not_run)
    body = _request("tools/call", {"name": "recall"})
    send = await _post(body, _headers(body, **{"mcp-name": name_header}))

    assert send.status == 400
    assert send.body["error"]["code"] == -32020


@pytest.mark.asyncio
async def test_base64_sentinel_name_header_is_decoded_before_comparison(monkeypatch):
    import mcp_server.tools as tools_mod

    async def fake_execute(**_kwargs):
        return []

    monkeypatch.setattr(tools_mod, "execute_tool_call", fake_execute)
    encoded = "=?base64?" + base64.b64encode(b"recall").decode() + "?="
    body = _request("tools/call", {"name": "recall"})
    send = await _post(body, _headers(body, **{"mcp-name": encoded}))

    assert send.status == 200


# ---------------------------------------- tolerated gaps (deliberate leniency)
# The spec makes the mirrored headers and ``clientCapabilities`` MUSTs. Nothing
# here routes on the headers or relies on a client capability, and the clients
# that matter can only be exercised in production — so their *absence* is
# served (and logged), while any *disagreement* above is still rejected.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "present",
    [
        [],
        ["mcp-protocol-version"],
        ["mcp-method"],
    ],
)
async def test_absent_mirrored_headers_are_tolerated_and_logged(monkeypatch, caplog, present):
    import mcp_server.tools as tools_mod

    async def fake_execute(**_kwargs):
        return []

    monkeypatch.setattr(tools_mod, "execute_tool_call", fake_execute)
    body = _request("tools/call", {"name": "recall"})
    headers = {k: v for k, v in _headers(body).items() if k.decode() in present}

    with caplog.at_level("WARNING", logger="mcp_server.transport_stateless"):
        send = await _post(body, headers)

    assert send.status == 200
    assert "Mcp-Name" in caplog.text  # never sent in any of the cases above


@pytest.mark.asyncio
async def test_absent_client_capabilities_is_tolerated_and_logged(caplog):
    body = _request("ping")
    del body["params"]["_meta"][CAPS_KEY]

    with caplog.at_level("WARNING", logger="mcp_server.transport_stateless"):
        send = await _post(body)

    assert send.status == 200
    assert CAPS_KEY in caplog.text


@pytest.mark.asyncio
async def test_fully_conforming_request_logs_no_metadata_warning(caplog):
    with caplog.at_level("WARNING", logger="mcp_server.transport_stateless"):
        send = await _post(_request("ping"))

    assert send.status == 200
    assert caplog.text == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover"},
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": {}}},
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": [1]},
    ],
)
async def test_bare_discover_probe_is_always_answered(body):
    """``server/discover`` is the pre-negotiation probe: a client that cannot
    read ``supportedVersions`` has nothing to correct its request with."""
    send = await _post(body, {})

    assert send.status == 200
    _assert_stateless(send)
    assert MODERN in send.body["result"]["supportedVersions"]


@pytest.mark.asyncio
async def test_discover_for_an_unsupported_version_still_names_the_supported_ones():
    send = await _post(_request("server/discover", version="2027-01-01"), {})

    assert send.status == 400
    assert send.body["error"]["code"] == -32022
    assert MODERN in send.body["error"]["data"]["supported"]


# ------------------------------------------------------------ malformed _meta


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "meta",
    [
        {CAPS_KEY: {}},  # protocolVersion missing on a non-discover method
        {PV_KEY: 20260728, CAPS_KEY: {}},  # wrong types
        {PV_KEY: "", CAPS_KEY: {}},
        {PV_KEY: MODERN, CAPS_KEY: []},
    ],
)
async def test_malformed_meta_is_400_invalid_params(meta):
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": meta}}
    send = await _post(
        body, {b"mcp-protocol-version": MODERN.encode(), b"mcp-method": b"tools/list"}
    )

    assert send.status == 400
    _assert_stateless(send)
    assert send.body["id"] == 2
    assert send.body["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_client_info_is_optional():
    body = _request("ping")
    del body["params"]["_meta"][INFO_KEY]
    send = await _post(body)
    assert send.status == 200


# ------------------------------------------------------------ envelope guards


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [None, 5, ""])
async def test_request_without_a_string_method_is_an_invalid_request(method):
    body = _request("ping")
    body["method"] = method
    send = await _post(body, {b"mcp-protocol-version": MODERN.encode()})

    assert send.status == 400
    assert send.body["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_invalid_id_is_never_echoed_even_without_a_method():
    body = _request("ping", request_id=True)
    del body["method"]
    send = await _post(body, {})

    assert send.status == 400
    assert send.body["error"]["code"] == -32600
    assert send.body["id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [None, True, 1.5, [1], {"a": 1}])
async def test_request_id_must_be_a_string_or_integer(request_id):
    """Unlike base JSON-RPC, an MCP request id MUST NOT be null."""
    send = await _post(_request("ping", request_id=request_id))

    assert send.status == 400
    assert send.body["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_notification_is_accepted_with_202_and_no_body():
    body = _request("notifications/cancelled")
    del body["id"]
    send = await _post(body)

    assert send.status == 202
    _assert_stateless(send)
    assert send.messages[1]["body"] == b""


# ------------------------------------------------------------------ era detection


_BARE_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.mark.parametrize(
    ("body", "modern"),
    [
        # Per-request _meta is the modern signal.
        (_request("tools/list"), True),
        (_request("server/discover"), True),
        # ... even for a version we do not support: it must reach the stateless
        # path to be told -32022 rather than being served under legacy semantics.
        (_request("tools/list", version="2027-01-01"), True),
        # server/discover exists only in the modern protocol — a bare probe must
        # not be answered from the session path (orphan Mcp-Session-Id).
        ({"jsonrpc": "2.0", "id": 1, "method": "server/discover"}, True),
        # initialize ALWAYS selects legacy semantics.
        (_request("initialize"), False),
        # A legacy request may carry _meta (progressToken) without the version key.
        (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "x", "_meta": {"progressToken": 1}},
            },
            False,
        ),
        (_BARE_LIST, False),
        # Unparseable / non-object bodies stay on the legacy path, which already
        # answers them with -32700 / -32600.
        (None, False),
        ([_request("tools/list")], False),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [1]}, False),
    ],
)
def test_is_modern_request(body, modern):
    assert _is_modern_request(body) is modern


# --------------------------------------------------- era split in mcp_asgi_app


class _SessionManagerSpy:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._sessions: dict = {}

    async def get_or_create_session(self, **_kwargs):
        self.calls.append("get_or_create_session")
        return SimpleNamespace(session_id="sess-1", user_id="user-1", workspace_id=None)

    async def get_session(self, session_id):
        self.calls.append(f"get_session:{session_id}")
        return None


@pytest.fixture
def asgi(monkeypatch):
    """``mcp_asgi_app`` with auth stubbed and the session manager spied on."""
    spy = _SessionManagerSpy()

    async def fake_auth(**_kwargs):
        return "user-1", None, None

    async def fake_workspace(_user_id):
        return None

    monkeypatch.setattr(transport, "authenticate_mcp_request", fake_auth)
    monkeypatch.setattr(transport, "_get_user_workspace_id", fake_workspace)
    monkeypatch.setattr(transport, "get_session_manager", lambda: spy)

    async def call(body: dict, headers: dict[bytes, bytes], path: str = "/mcp/") -> _Recorder:
        raw = json.dumps(body).encode()
        chunks = [raw[: len(raw) // 2], raw[len(raw) // 2 :]]

        async def receive():
            if chunks:
                chunk = chunks.pop(0)
                return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}
            return {"type": "http.disconnect"}

        send = _Recorder()
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": b"",
            "headers": list(headers.items()),
        }
        await mcp_asgi_app(scope, receive, send)
        return send

    return SimpleNamespace(call=call, sessions=spy)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/mcp/", "/mcp"])
async def test_modern_request_is_served_without_touching_a_session(asgi, path):
    """``/mcp/`` is what production sees (the FastAPI route normalizes to it)."""
    body = _request("server/discover")
    send = await asgi.call(body, _headers(body), path)

    assert send.status == 200
    _assert_stateless(send)
    assert MODERN in send.body["result"]["supportedVersions"]
    assert asgi.sessions.calls == []


@pytest.mark.asyncio
async def test_modern_request_ignores_a_stale_mcp_session_id(asgi):
    """2026-07-28: "An Mcp-Session-Id header on a request: ignore it." The legacy
    path would answer an unknown id with 404 'session not found'."""
    body = _request("ping")
    headers = _headers(body)
    headers[b"mcp-session-id"] = b"mcp-long-gone"
    send = await asgi.call(body, headers)

    assert send.status == 200
    _assert_stateless(send)
    assert asgi.sessions.calls == []


@pytest.mark.asyncio
async def test_modern_error_responses_never_mint_a_session(asgi):
    body = _request("tools/list", version="2027-01-01")
    send = await asgi.call(body, _headers(body))

    assert send.status == 400
    assert send.body["error"]["code"] == -32022
    assert asgi.sessions.calls == []


@pytest.mark.asyncio
async def test_initialize_still_gets_a_session_and_its_replayed_body(asgi):
    """The era split reads the body first; the legacy handler must still see it."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
    }
    send = await asgi.call(body, {b"mcp-protocol-version": b"2025-03-26"})

    assert send.status == 200
    assert send.headers[b"mcp-session-id"] == b"sess-1"
    assert send.body["result"]["protocolVersion"] == "2025-03-26"
    assert asgi.sessions.calls == ["get_or_create_session"]


@pytest.mark.asyncio
async def test_initialize_never_negotiates_the_modern_revision(asgi):
    """2026-07-28 has no handshake; echoing it from ``initialize`` would promise
    a session-scoped modern protocol that does not exist."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": MODERN, "_meta": _meta()},
    }
    send = await asgi.call(body, {b"mcp-protocol-version": MODERN.encode()})

    assert send.status == 200
    assert send.body["result"]["protocolVersion"] in LEGACY_PROTOCOL_VERSIONS


@pytest.mark.asyncio
async def test_legacy_request_without_modern_metadata_keeps_the_legacy_contract(asgi):
    body = {"jsonrpc": "2.0", "id": 7, "method": "resources/list", "params": {}}
    send = await asgi.call(body, {})

    # 200 + -32601, NOT the modern 404: a dual-era client must still be able to
    # read this server as legacy-capable and fall back to ``initialize``.
    assert send.status == 200
    assert send.body["error"]["code"] == -32601
    assert send.headers[b"mcp-session-id"] == b"sess-1"


@pytest.mark.asyncio
async def test_modern_version_header_alone_does_not_pull_a_request_off_its_session(asgi):
    """The header is not an era signal: a proxy or SDK stamping its newest known
    version on every request must not turn a working session call into a
    stateless rejection."""
    body = {"jsonrpc": "2.0", "id": 3, "method": "ping"}
    send = await asgi.call(body, {b"mcp-protocol-version": MODERN.encode()})

    assert send.status == 200
    assert send.body == {"jsonrpc": "2.0", "id": 3, "result": {}}
    assert send.headers[b"mcp-session-id"] == b"sess-1"


@pytest.mark.asyncio
async def test_bare_discover_probe_mints_no_session(asgi):
    """A DiscoverResult advertising a session-less revision next to a fresh
    ``Mcp-Session-Id`` is self-contradictory — and every probe used to leave an
    orphan session behind until the idle timeout."""
    send = await asgi.call({"jsonrpc": "2.0", "id": 1, "method": "server/discover"}, {})

    assert send.status == 200
    _assert_stateless(send)
    assert MODERN in send.body["result"]["supportedVersions"]
    assert asgi.sessions.calls == []


@pytest.mark.asyncio
async def test_invalid_utf8_body_is_a_parse_error_not_a_500(asgi):
    async def receive():
        return {"type": "http.request", "body": b'{"method": "\xff"}', "more_body": False}

    send = _Recorder()
    scope = {"type": "http", "method": "POST", "path": "/mcp/", "query_string": b"", "headers": []}
    await mcp_asgi_app(scope, receive, send)

    assert send.status == 400
    assert send.body["error"]["code"] == -32700
