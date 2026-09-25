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
from unittest.mock import patch

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


async def _post(
    body: dict, headers: dict[bytes, bytes] | None = None, *, query_string: bytes = b""
) -> _Recorder:
    send = _Recorder()
    await handle_stateless_post(
        send,
        body,
        _headers(body) if headers is None else headers,
        user_id="user-1",
        workspace_id=None,
        query_string=query_string,
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
    # #1601: tools/list reads the registry through the tool-profile selector.
    import mcp_server.tools._profiles as profiles_mod

    monkeypatch.setattr(
        profiles_mod, "get_tool_definitions", lambda: [{"name": "recall", "inputSchema": {}}]
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
    # #1622: success carries no ``isError`` key ("if not set ... false").
    assert set(result) == {"resultType", "content", "_meta"}
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
async def test_tools_call_error_envelope_sets_is_error_and_keeps_content():
    """#1622: a tool *execution* error is a result flagged ``isError: true``
    (MCP 2026-07-28, Tools → Error Handling); the envelope text is unchanged.
    Drives the real ``execute_tool_call``: the context-id check fails before
    any DB access."""
    from mcp_server.tools._helpers import _error_response

    send = await _post(
        _request(
            "tools/call",
            {"name": "recall", "arguments": {"context_id": "not-a-uuid", "query": "x"}},
        )
    )

    assert send.status == 200
    _assert_stateless(send)
    assert "error" not in send.body  # a result, not a protocol error
    result = send.body["result"]
    assert result["resultType"] == "complete"
    assert result["isError"] is True
    assert result["_meta"][SERVER_INFO_KEY]["name"] == "kagura-memory-cloud"
    envelope = json.loads(result["content"][0]["text"])
    assert envelope["error"] == "invalid_context_id_format"
    expected = _error_response("invalid_context_id_format", envelope["message"])[0].text
    assert result["content"] == [{"type": "text", "text": expected}]


@pytest.mark.asyncio
async def test_unknown_tool_envelope_is_flagged():
    send = await _post(_request("tools/call", {"name": "no_such_tool", "arguments": {}}))

    result = send.body["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["error"] == "unknown_tool"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "code", "error_code"),
    [
        (ValueError("bad arg"), -32602, "validation_error"),
        # A ValueError *subclass* is a server failure; the code agrees with data.
        (json.JSONDecodeError("Expecting value", "/srv/app/x.json", 0), -32603, "internal_error"),
        (PermissionError("nope"), -32603, "permission_denied"),
        (TimeoutError(), -32603, "timeout"),
        (RuntimeError("secret dsn"), -32603, "internal_error"),
    ],
)
async def test_tools_call_failure_uses_only_standard_jsonrpc_codes(
    monkeypatch, exc, code, error_code
):
    """-32000..-32019 is the spec's *legacy* sub-range (SHOULD NOT be used by a
    2026-07-28 implementation), so the modern path does not reuse the legacy
    path's custom -32001 / -32002. ``data`` carries the #1684 vocabulary code,
    never the exception's type or text."""
    import mcp_server.tools as tools_mod

    async def boom(**_kwargs):
        raise exc

    monkeypatch.setattr(tools_mod, "execute_tool_call", boom)
    send = await _post(_request("tools/call", {"name": "list_contexts"}, request_id=6))

    assert send.status == 200
    _assert_stateless(send)
    error = send.body["error"]
    assert send.body["id"] == 6
    assert error["code"] == code
    assert error["data"]["error"] == error_code
    assert error["data"]["help"]
    assert "exception_type" not in error["data"]
    assert "details" not in error["data"]


@pytest.mark.asyncio
async def test_unexpected_tool_exception_text_is_not_echoed_to_the_client(monkeypatch):
    """#1684: a DSN / path in the exception reaches the server log (with the
    correlation_id the client gets), never the JSON-RPC error."""
    import mcp_server.tools as tools_mod

    exc = RuntimeError("postgresql://user:hunter2@db/prod at /srv/app/secret.py")

    async def boom(**_kwargs):
        raise exc

    monkeypatch.setattr(tools_mod, "execute_tool_call", boom)
    with patch("mcp_server.tools._errors.logger") as log:
        send = await _post(_request("tools/call", {"name": "list_contexts"}))

    wire = json.dumps(send.body)
    assert "hunter2" not in wire
    assert "/srv/app" not in wire
    assert "RuntimeError" not in wire
    data = send.body["error"]["data"]
    assert data["error"] == "internal_error"
    assert data["retryable"] is True  # list_contexts only reads
    event = log.error.call_args
    assert event.kwargs["exc_info"] is exc
    assert event.kwargs["correlation_id"] == data["correlation_id"]


@pytest.mark.asyncio
async def test_transport_failure_on_a_write_tool_is_not_marked_retryable(monkeypatch):
    import mcp_server.tools as tools_mod

    async def boom(**_kwargs):
        raise TimeoutError()

    monkeypatch.setattr(tools_mod, "execute_tool_call", boom)
    send = await _post(_request("tools/call", {"name": "forget"}))

    data = send.body["error"]["data"]
    assert data["error"] == "timeout"
    assert data["retryable"] is False
    assert data["outcome"] == "unknown"
    assert "retry_after_seconds" not in data
    assert "reference" in data["help"]  # the read that shows whether forget took effect


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


@pytest.mark.asyncio
async def test_modern_request_inherits_the_workspace_url_check(monkeypatch):
    """The era split sits AFTER authentication and the workspace checks: a
    stateless request must not be a way around the key↔URL workspace match."""
    from uuid import uuid4

    key_workspace = uuid4()

    async def fake_auth(**_kwargs):
        return "user-1", None, key_workspace

    async def must_not_run(*_args, **_kwargs):  # pragma: no cover - the assertion
        raise AssertionError("a workspace-mismatched request reached the stateless handler")

    import mcp_server.transport_stateless as stateless

    monkeypatch.setattr(transport, "authenticate_mcp_request", fake_auth)
    monkeypatch.setattr(stateless, "handle_stateless_post", must_not_run)

    body = _request("tools/list")
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    send = _Recorder()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/",
        "query_string": b"",
        "headers": list(_headers(body).items()),
        "workspace_id_from_url": str(uuid4()),
    }
    await mcp_asgi_app(scope, receive, send)

    assert send.status == 403
    assert send.body["error"] == "workspace_mismatch"


@pytest.mark.asyncio
async def test_unauthenticated_modern_request_gets_the_oauth_challenge(monkeypatch):
    """ChatGPT's first POST is an unauthenticated probe: it must still get the
    401 + WWW-Authenticate that starts the OAuth flow, whatever its era."""

    async def failing_auth(**_kwargs):
        raise Exception("Missing Authorization header")

    monkeypatch.setattr(transport, "authenticate_mcp_request", failing_auth)

    body = _request("server/discover")
    raw = json.dumps(body).encode()

    async def receive():  # pragma: no cover - auth fails before the body is read
        return {"type": "http.request", "body": raw, "more_body": False}

    send = _Recorder()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/",
        "query_string": b"",
        "headers": list(_headers(body).items()),
    }
    await mcp_asgi_app(scope, receive, send)

    assert send.status == 401
    assert b"www-authenticate" in send.headers


# ------------------------------------------------------- PR review follow-ups


@pytest.mark.asyncio
@pytest.mark.parametrize("method_header", [b"tools/list", b"=?base64?!!!not-base64!!!?="])
async def test_bare_discover_probe_with_a_contradicting_header_is_rejected(method_header):
    """Absence is tolerated even on the bare probe; contradiction never is."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}
    send = await _post(body, {b"mcp-method": method_header})

    assert send.status == 400
    assert send.body["error"]["code"] == -32020


@pytest.mark.asyncio
async def test_bare_discover_probe_with_a_matching_header_is_answered():
    body = {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}
    send = await _post(body, {b"mcp-method": b"server/discover"})

    assert send.status == 200
    assert MODERN in send.body["result"]["supportedVersions"]


@pytest.mark.asyncio
async def test_explicit_null_arguments_is_treated_as_omitted(monkeypatch):
    """Matches the reference SDK (``arguments: dict | None = None``): serializers
    with nullable fields emit ``null`` for a no-argument call."""
    import mcp_server.tools as tools_mod

    seen: dict = {}

    async def fake_execute(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(tools_mod, "execute_tool_call", fake_execute)
    send = await _post(_request("tools/call", {"name": "list_contexts", "arguments": None}))

    assert send.status == 200
    assert seen["arguments"] == {}


# ------------------------------------------------ guardrail digest (#1621)
# ``server/discover`` is the only ``instructions`` carrier on this era. The
# digest of one context's tool guardrails is appended when the URL selects it
# (``?guardrails=<uuid>``) or an agent-bound key's default binding does; every
# other case is the base text — and, with nothing selected, byte-identical
# public / 1 h bytes with zero database access.


def _digest_entries(context_id, *summaries: str, total: int | None = None):
    from uuid import uuid4

    from services.guardrail_digest import DigestEntries, DigestEntry

    items = [
        DigestEntry(
            memory_id=str(uuid4()),
            summary=s,
            importance=0.8,
            authored_by_caller=True,
            source_type="manual",
        )
        for s in summaries
    ]
    return DigestEntries(
        context_id=context_id,
        entries=items,
        total_available=len(items) if total is None else total,
        truncated=(total or len(items)) > len(items),
        tool_triggered_version="0123456789abcdef",
    )


@pytest.fixture
def digest_source(monkeypatch):
    """Fake the DB session and the entry source behind ``build_instructions``.

    ``get_db`` yields an ``AsyncMock`` session (so the statement-timeout
    ``execute`` is a no-op) and ``fetch_entries`` returns whatever the test
    installs; the spy records every ``get_db`` call.
    """
    from unittest.mock import AsyncMock

    import db.base as db_base
    import services.guardrail_digest as digest_mod

    state = SimpleNamespace(entries=None, db_calls=0, fetch_kwargs=[], raise_with=None, sleep=0.0)

    async def fake_get_db():
        state.db_calls += 1
        yield AsyncMock()

    async def fake_fetch_entries(db, **kwargs):
        import asyncio

        state.fetch_kwargs.append(kwargs)
        if state.sleep:
            await asyncio.sleep(state.sleep)
        if state.raise_with is not None:
            raise state.raise_with
        return state.entries

    monkeypatch.setattr(db_base, "get_db", fake_get_db)
    monkeypatch.setattr(digest_mod, "fetch_entries", fake_fetch_entries)
    return state


@pytest.fixture
def agent_scope():
    from uuid import uuid4

    from auth.agent_scope import AgentScope, set_agent_scope

    set_agent_scope(None)

    def _set(*, workspace_id=None):
        scope = AgentScope(agent_id=uuid4(), enforcement_mode="enforce", workspace_id=workspace_id)
        set_agent_scope(scope)
        return scope

    yield _set
    set_agent_scope(None)


@pytest.fixture(autouse=True)
def _no_ambient_agent_scope():
    from auth.agent_scope import set_agent_scope

    set_agent_scope(None)
    yield
    set_agent_scope(None)


@pytest.mark.asyncio
async def test_discover_without_selection_is_the_base_text_public_one_hour_and_db_free(
    digest_source,
):
    from mcp_server.transport import DISCOVER_TTL_MS, SERVER_INSTRUCTIONS_BASE

    send = await _post(_request("server/discover"))

    result = send.body["result"]
    assert result["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert result["cacheScope"] == "public"
    assert result["ttlMs"] == DISCOVER_TTL_MS
    assert digest_source.db_calls == 0
    assert digest_source.fetch_kwargs == []


@pytest.mark.asyncio
async def test_discover_with_a_selected_context_carries_a_private_digest(digest_source):
    from uuid import uuid4

    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE, TOOLS_LIST_TTL_MS
    from services.guardrail_digest import digest_header

    ctx = uuid4()
    digest_source.entries = _digest_entries(ctx, "never force-push a shared branch")
    qs = f"guardrails={ctx}".encode()

    first = await _post(_request("server/discover"), query_string=qs)
    second = await _post(_request("server/discover"), query_string=qs)

    result = first.body["result"]
    assert result["instructions"].startswith(SERVER_INSTRUCTIONS_BASE + "\n\n" + digest_header(ctx))
    assert "never force-push a shared branch" in result["instructions"]
    assert result["cacheScope"] == "private"
    assert result["ttlMs"] == TOOLS_LIST_TTL_MS <= 300_000
    # Same caller, same URL → byte-identical.
    assert second.body["result"] == result
    # The URL's context reached the entry source with the pure key scope.
    assert digest_source.fetch_kwargs[0]["context_id"] == ctx
    assert digest_source.fetch_kwargs[0]["key_workspace_id"] is None
    assert digest_source.db_calls == 2


@pytest.mark.asyncio
async def test_digest_suffix_follows_the_urls_tool_profile(digest_source):
    from uuid import uuid4

    ctx = uuid4()
    digest_source.entries = _digest_entries(ctx, "a", total=4)

    full = await _post(_request("server/discover"), query_string=f"guardrails={ctx}".encode())
    core = await _post(
        _request("server/discover"),
        query_string=f"guardrails={ctx}&profile=core".encode(),
    )

    assert full.body["result"]["instructions"].endswith("(+3 more: load_guardrails(context_id))")
    assert core.body["result"]["instructions"].endswith("(+3 more: get_context_info(context_id))")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["off", "OFF", "%20off"])
async def test_guardrails_off_is_the_base_text_with_private_hints_and_no_db(digest_source, value):
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE, TOOLS_LIST_TTL_MS

    send = await _post(_request("server/discover"), query_string=f"guardrails={value}".encode())

    result = send.body["result"]
    assert result["instructions"] == SERVER_INSTRUCTIONS_BASE
    # A selection was attempted: the result may vary by URL → never public.
    assert result["cacheScope"] == "private"
    assert result["ttlMs"] == TOOLS_LIST_TTL_MS
    assert digest_source.db_calls == 0


@pytest.mark.asyncio
async def test_guardrails_typo_is_ignored_logged_without_its_bytes_and_private(
    digest_source, caplog
):
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE

    with caplog.at_level("INFO", logger="mcp_server.transport"):
        send = await _post(
            _request("server/discover"),
            query_string=b"guardrails=kagura_not_a_uuid_value",
        )

    result = send.body["result"]
    assert result["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert result["cacheScope"] == "private"
    assert digest_source.db_calls == 0
    assert "mcp_guardrails_param_ignored" in caplog.text
    assert "parsed=False" in caplog.text
    assert "kagura_not_a_uuid_value" not in caplog.text


@pytest.mark.asyncio
async def test_denied_or_unknown_context_gives_the_same_bytes_as_no_selection(
    digest_source,
):
    """``fetch_entries`` returns ``None`` on every deny (unknown, other
    workspace, private non-creator, not a member) and an empty set for an
    external-tier context: both serve exactly the no-selection ``instructions``
    — no error, no signal — but the hints stay private (attempted)."""
    from uuid import uuid4

    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE

    ctx = uuid4()
    plain = await _post(_request("server/discover"))

    digest_source.entries = None
    denied = await _post(_request("server/discover"), query_string=f"guardrails={ctx}".encode())
    digest_source.entries = _digest_entries(ctx)  # external tier / nothing marked
    empty = await _post(_request("server/discover"), query_string=f"guardrails={ctx}".encode())

    for send in (denied, empty):
        assert send.status == 200
        assert send.body["result"]["instructions"] == plain.body["result"]["instructions"]
        assert send.body["result"]["instructions"] == SERVER_INSTRUCTIONS_BASE
        assert send.body["result"]["cacheScope"] == "private"


@pytest.mark.asyncio
async def test_agent_default_binding_selects_the_digest_without_the_parameter(
    digest_source, agent_scope, monkeypatch
):
    from uuid import uuid4

    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE
    from services.agent_binding_service import AgentBindingService

    ctx = uuid4()
    scope = agent_scope()
    seen: dict = {}

    async def fake_resolve(self, agent_id):
        seen["agent_id"] = agent_id
        return SimpleNamespace(context_id=ctx), "default"

    monkeypatch.setattr(AgentBindingService, "resolve_default_binding", fake_resolve)
    digest_source.entries = _digest_entries(ctx, "bound lesson")

    send = await _post(_request("server/discover"))

    result = send.body["result"]
    assert "bound lesson" in result["instructions"]
    assert result["cacheScope"] == "private"
    assert seen["agent_id"] == scope.agent_id
    assert digest_source.fetch_kwargs[0]["context_id"] == ctx

    # ... and the same key with ``?guardrails=off`` gets the base text, no DB.
    digest_source.db_calls = 0
    off = await _post(_request("server/discover"), query_string=b"guardrails=off")
    assert off.body["result"]["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert off.body["result"]["cacheScope"] == "private"
    assert digest_source.db_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["none", "ambiguous"])
async def test_agent_without_a_default_binding_gets_the_base_text_privately(
    digest_source, agent_scope, monkeypatch, outcome
):
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE
    from services.agent_binding_service import AgentBindingService

    agent_scope()

    async def fake_resolve(self, agent_id):
        return None, outcome

    monkeypatch.setattr(AgentBindingService, "resolve_default_binding", fake_resolve)
    digest_source.entries = _digest_entries(None, "must not appear")

    send = await _post(_request("server/discover"))

    assert send.body["result"]["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert send.body["result"]["cacheScope"] == "private"
    assert digest_source.fetch_kwargs == []  # no context → no read, no oracle


@pytest.mark.asyncio
async def test_resolver_deny_for_an_agent_credential_is_fail_open_and_audited_by_operation(
    agent_scope, monkeypatch
):
    """The real entry source runs here (only the DB session and the resolver
    are faked): a deny is ``None`` → base text, request 200, private hints.
    The resolver is called with ``operation="load_guardrails"`` — the MAE
    vocabulary value under which ``PermissionService`` persists the deny row
    for agent credentials (the row itself is asserted against the DB in
    ``tests/integration/test_guardrail_digest_repo.py``)."""
    from unittest.mock import AsyncMock
    from uuid import uuid4

    import db.base as db_base
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE
    from services.permission_service import PermissionService
    from utils.exceptions import NotFoundException

    agent_scope(workspace_id=uuid4())
    ctx = uuid4()
    seen: dict = {}

    async def fake_get_db():
        yield AsyncMock()

    async def deny(self, **kwargs):
        seen.update(kwargs)
        raise NotFoundException("Context", str(kwargs["context_id"]))

    monkeypatch.setattr(db_base, "get_db", fake_get_db)
    monkeypatch.setattr(PermissionService, "resolve_context_for_workspace_read", deny)

    send = await _post(_request("server/discover"), query_string=f"guardrails={ctx}".encode())

    assert send.status == 200
    assert send.body["result"]["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert send.body["result"]["cacheScope"] == "private"
    assert seen["operation"] == "load_guardrails"
    assert seen["context_id"] == ctx
    assert seen["required_role"] == "viewer"


@pytest.mark.asyncio
async def test_entry_source_failure_serves_the_base_text_and_succeeds(digest_source, caplog):
    from uuid import uuid4

    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE

    digest_source.raise_with = RuntimeError("postgresql://user:hunter2@db/prod")

    with caplog.at_level("WARNING", logger="mcp_server.transport"):
        send = await _post(
            _request("server/discover"), query_string=f"guardrails={uuid4()}".encode()
        )

    assert send.status == 200
    assert send.body["result"]["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert "mcp_guardrail_digest_failed" in caplog.text
    assert "reason=RuntimeError" in caplog.text
    assert "hunter2" not in caplog.text and "hunter2" not in json.dumps(send.body)


@pytest.mark.asyncio
async def test_entry_source_past_the_budget_serves_the_base_text(
    digest_source, monkeypatch, caplog
):
    from uuid import uuid4

    from config.settings import get_settings
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE

    monkeypatch.setattr(get_settings(), "mcp_guardrail_digest_timeout_ms", 10)
    digest_source.sleep = 0.5
    digest_source.entries = _digest_entries(uuid4(), "too late")

    with caplog.at_level("WARNING", logger="mcp_server.transport"):
        send = await _post(
            _request("server/discover"), query_string=f"guardrails={uuid4()}".encode()
        )

    assert send.status == 200
    assert send.body["result"]["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert "reason=TimeoutError" in caplog.text


@pytest.mark.asyncio
async def test_feature_flag_off_serves_the_base_text_without_touching_the_db(
    digest_source, monkeypatch
):
    from uuid import uuid4

    from config.settings import get_settings
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE

    monkeypatch.setattr(get_settings(), "mcp_guardrail_digest_enabled", False)
    digest_source.entries = _digest_entries(uuid4(), "flag is off")

    send = await _post(_request("server/discover"), query_string=f"guardrails={uuid4()}".encode())

    assert send.body["result"]["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert send.body["result"]["cacheScope"] == "private"  # still attempted
    assert digest_source.db_calls == 0


@pytest.mark.asyncio
async def test_client_info_never_changes_the_instructions_bytes(digest_source):
    from uuid import uuid4

    ctx = uuid4()
    digest_source.entries = _digest_entries(ctx, "same for everyone")
    qs = f"guardrails={ctx}".encode()

    a = _request("server/discover")
    b = _request("server/discover")
    b["params"]["_meta"][INFO_KEY] = {"name": "openai-mcp", "version": "9.9"}
    bare = {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}

    sends = [
        await _post(a, query_string=qs),
        await _post(b, query_string=qs),
        await _post(bare, {}, query_string=qs),
    ]
    texts = {s.body["result"]["instructions"] for s in sends}
    assert len(texts) == 1


@pytest.mark.asyncio
async def test_asgi_app_stores_the_selection_and_passes_the_query_string(asgi, monkeypatch):
    """The era split hands the raw query string to the stateless handler and
    the ``?guardrails=`` selection is parsed once per request into the
    contextvar the ``get_context_info`` handler reads."""
    from mcp_server.tools import _helpers

    seen: dict = {}
    original = _helpers.set_mcp_guardrails_selection

    def spy(selection):
        seen["selection"] = selection
        original(selection)

    monkeypatch.setattr(_helpers, "set_mcp_guardrails_selection", spy)

    body = _request("server/discover")
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    send = _Recorder()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/",
        "query_string": b"guardrails=off",
        "headers": list(_headers(body).items()),
    }
    await mcp_asgi_app(scope, receive, send)

    assert send.status == 200
    assert seen["selection"].mode == "off"
    assert send.body["result"]["cacheScope"] == "private"
