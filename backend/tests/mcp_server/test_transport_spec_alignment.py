"""Streamable HTTP spec alignment for ``/mcp`` (#1740).

The Anthropic Software Directory re-audit found transport gaps against the MCP
Streamable HTTP spec. Covered here:

* **Origin** — validated before authentication on every request. No ``Origin``
  (server-side clients) passes; one that is not allow-listed (the CORS origins,
  the ``FRONTEND_URL`` origin, ``MCP_ALLOWED_ORIGINS``), or ``null``, is 403
  with a JSON-RPC error body.
* **Terminated sessions** — an id ended by ``DELETE`` is tombstoned for the
  idle timeout and answered 404. The deliberate re-adoption of an unknown id
  (#1686: idle cleanup, restart, deploy) stays, but only for ids in the
  server-minted format; a client-invented id is 404.
* **Versions** — ``initialize`` answers a revision it does not implement with
  the latest legacy one (2025-03-26), not 2024-11-05.
* **Batches** — a session that negotiated 2025-03-26 accepts JSON-RPC batch
  arrays (a MUST of that revision); a 2024-11-05 session keeps refusing them.

``mcp_asgi_app`` runs with authentication and the workspace lookup stubbed and
the real ``MCPSessionManager``; the tool dispatch is stubbed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import mcp_server.tools as tools_mod
import mcp_server.transport as transport
from config.settings import get_settings
from mcp_server.session import MCPSessionManager, is_server_minted_session_id
from mcp_server.transport import mcp_asgi_app

FRONTEND = "https://memory.example.com"


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
    def body(self):
        return json.loads(self.raw)


@pytest.fixture
def app(monkeypatch):
    """``mcp_asgi_app`` with auth stubbed, a real session manager and a
    recording tool dispatch."""
    settings = get_settings()
    monkeypatch.setattr(settings, "frontend_url", FRONTEND)
    monkeypatch.setattr(settings, "cors_origins", "http://localhost:3000,http://localhost:8080")
    monkeypatch.setattr(settings, "mcp_allowed_origins", "")

    state = SimpleNamespace(
        manager=MCPSessionManager(), executed=[], authenticated=0, user="user-1"
    )

    async def fake_auth(**_kwargs):
        state.authenticated += 1
        return state.user, None, None

    async def no_workspace(_user_id):
        return None

    async def execute_tool_call(**kwargs):
        state.executed.append(kwargs["tool_name"])
        return [SimpleNamespace(type="text", text='{"status":"success"}')]

    monkeypatch.setattr(transport, "authenticate_mcp_request", fake_auth)
    monkeypatch.setattr(transport, "_get_user_workspace_id", no_workspace)
    monkeypatch.setattr(transport, "get_session_manager", lambda: state.manager)
    monkeypatch.setattr(tools_mod, "execute_tool_call", execute_tool_call)

    async def call(
        body,
        *,
        headers: dict[bytes, bytes] | None = None,
        method: str = "POST",
        path: str = "/mcp/",
    ) -> _Recorder:
        raw = json.dumps(body).encode() if body is not None else b""

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        send = _Recorder()
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": list((headers or {}).items()),
        }
        await mcp_asgi_app(scope, receive, send)
        return send

    state.call = call
    return state


def _rpc(method: str, request_id: int | None = 1, **params) -> dict:
    body: dict = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        body["id"] = request_id
    if params:
        body["params"] = params
    return body


async def _initialize(app, version: str = "2025-03-26") -> dict[bytes, bytes]:
    opened = await app.call(_rpc("initialize", protocolVersion=version))
    assert opened.status == 200
    return {b"mcp-session-id": opened.headers[b"mcp-session-id"]}


# ---------------------------------------------------------------------- Origin


@pytest.mark.asyncio
async def test_a_request_without_origin_is_served(app):
    """Server-side clients (the Claude and ChatGPT backends) send no Origin."""
    send = await app.call(_rpc("initialize", protocolVersion="2025-03-26"))
    assert send.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        FRONTEND,
        "https://MEMORY.example.com",  # host case is not significant
        "https://memory.example.com:443",  # default port
        "http://localhost:3000",  # CORS_ORIGINS
    ],
)
async def test_an_allow_listed_origin_is_served(app, origin):
    send = await app.call(
        _rpc("initialize", protocolVersion="2025-03-26"), headers={b"origin": origin.encode()}
    )
    assert send.status == 200


@pytest.mark.asyncio
async def test_mcp_allowed_origins_extends_the_allow_list(app, monkeypatch):
    monkeypatch.setattr(
        get_settings(), "mcp_allowed_origins", "https://tools.example.org, https://x.example.net"
    )
    for origin in (b"https://tools.example.org", b"https://x.example.net"):
        send = await app.call(_rpc("ping", 2), headers={b"origin": origin})
        # ping without a session opens one; what matters is it is not a 403
        assert send.status == 200, origin


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        b"https://evil.example",
        b"null",
        b"https://memory.example.com.evil.example",
        b"http://memory.example.com",  # scheme is part of the origin
        b"https://memory.example.com:8443",
        b"not a url",
        b"",
        b"\xff\xfe",
    ],
)
@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/mcp/"), ("GET", "/mcp/"), ("DELETE", "/mcp/"), ("POST", "/mcp")],
)
async def test_an_invalid_origin_is_403_before_authentication(app, origin, method, path):
    body = _rpc("tools/call", 3, name="recall", arguments={}) if method == "POST" else None
    send = await app.call(
        body,
        headers={b"origin": origin, b"mcp-session-id": b"mcp-0123456789abcdef"},
        method=method,
        path=path,
    )

    assert send.status == 403
    assert send.headers[b"content-type"] == b"application/json"
    payload = send.body
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] is None
    assert payload["error"]["code"] == -32600
    assert "Origin" in payload["error"]["message"]
    # Refused before anything ran: no authentication, no session, no tool.
    assert app.authenticated == 0
    assert app.manager._sessions == {}
    assert app.executed == []


@pytest.mark.asyncio
async def test_a_wildcard_cors_entry_does_not_allow_every_origin(app, monkeypatch):
    monkeypatch.setattr(get_settings(), "cors_origins", "*")
    send = await app.call(_rpc("ping", 2), headers={b"origin": b"https://evil.example"})
    assert send.status == 403


# --------------------------------------------------------- terminated sessions


def test_server_minted_ids_have_the_documented_format():
    minted = {MCPSessionManager().generate_session_id() for _ in range(20)}
    assert all(is_server_minted_session_id(sid) for sid in minted)
    assert not is_server_minted_session_id("mcp-gone")
    assert not is_server_minted_session_id("mcp-0123456789ABCDEF")
    assert not is_server_minted_session_id("mcp-0123456789abcdef0")
    assert not is_server_minted_session_id("550e8400-e29b-41d4-a716-446655440000")


@pytest.mark.asyncio
async def test_a_session_ended_by_delete_is_404_afterwards(app):
    session = await _initialize(app)

    ended = await app.call(None, headers=session, method="DELETE")
    assert ended.status == 204

    for method, body in (("POST", _rpc("ping", 2)), ("GET", None), ("DELETE", None)):
        again = await app.call(body, headers=session, method=method)
        assert again.status == 404, method
        assert again.body["error"]["data"]["session_id"] == session[b"mcp-session-id"].decode()
    assert app.manager._sessions == {}


@pytest.mark.asyncio
async def test_a_terminated_id_cannot_be_opened_by_another_caller(app):
    session = await _initialize(app)
    await app.call(None, headers=session, method="DELETE")

    app.user = "user-2"
    send = await app.call(_rpc("ping", 2), headers=session)
    assert send.status == 404
    assert app.manager._sessions == {}


@pytest.mark.asyncio
async def test_a_tombstone_expires_with_the_idle_timeout(app):
    from datetime import timedelta

    from utils.datetime import utcnow

    session = await _initialize(app)
    sid = session[b"mcp-session-id"].decode()
    await app.call(None, headers=session, method="DELETE")

    # Past the idle timeout the id is indistinguishable from one lost to idle
    # cleanup or a restart, and is re-adopted like one (#1686).
    app.manager._tombstones[sid] = utcnow() - timedelta(seconds=1)
    send = await app.call(_rpc("ping", 2), headers=session)
    assert send.status == 200
    assert send.headers[b"mcp-session-id"] == sid.encode()


@pytest.mark.asyncio
async def test_expired_tombstones_are_pruned_by_the_idle_cleanup(app):
    from datetime import timedelta

    from utils.datetime import utcnow

    app.manager._tombstones["mcp-0123456789abcdef"] = utcnow() - timedelta(seconds=1)
    app.manager._tombstones["mcp-fedcba9876543210"] = utcnow() + timedelta(hours=1)
    await app.manager.cleanup_inactive_sessions(timeout_seconds=3600)
    assert list(app.manager._tombstones) == ["mcp-fedcba9876543210"]


@pytest.mark.asyncio
async def test_the_tombstone_map_is_bounded():
    manager = MCPSessionManager(max_tombstones=3)
    for i in range(5):
        session = await manager.get_or_create_session(user_id="user-1")
        await manager.terminate_session(session.session_id)
        assert len(manager._tombstones) <= 3, i
    assert len(manager._tombstones) == 3


@pytest.mark.asyncio
async def test_an_unknown_server_minted_id_is_still_re_adopted(app):
    """#1686: idle cleanup and restarts drop sessions; a client that cannot
    re-initialize after a 404 keeps working."""
    send = await app.call(_rpc("ping", 2), headers={b"mcp-session-id": b"mcp-0123456789abcdef"})
    assert send.status == 200
    assert send.headers[b"mcp-session-id"] == b"mcp-0123456789abcdef"
    assert app.manager._sessions["mcp-0123456789abcdef"].user_id == "user-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_id",
    [
        b"mcp-gone",
        b"client-chosen",
        b"550e8400-e29b-41d4-a716-446655440000",
        b"mcp-0123456789ABCDEF",
    ],
)
@pytest.mark.parametrize("method", ["POST", "GET"])
async def test_a_client_invented_id_is_404_and_never_adopted(app, session_id, method):
    body = _rpc("ping", 2) if method == "POST" else None
    send = await app.call(body, headers={b"mcp-session-id": session_id}, method=method)

    assert send.status == 404
    assert send.body["error"]["data"]["session_id"] == session_id.decode()
    assert app.manager._sessions == {}


# -------------------------------------------------------------------- versions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "negotiated"),
    [
        ("2025-03-26", "2025-03-26"),
        ("2024-11-05", "2024-11-05"),  # still echoed to clients that ask for it
        ("2025-06-18", "2025-03-26"),  # newer than we implement → latest legacy
        ("2025-11-25", "2025-03-26"),
        ("2026-07-28", "2025-03-26"),  # modern: no handshake form
        ("1999-01-01", "2025-03-26"),
    ],
)
async def test_initialize_negotiates_the_latest_legacy_version_it_implements(
    app, requested, negotiated
):
    send = await app.call(_rpc("initialize", protocolVersion=requested))
    assert send.status == 200
    assert send.body["result"]["protocolVersion"] == negotiated
    sid = send.headers[b"mcp-session-id"].decode()
    assert app.manager._sessions[sid].protocol_version == negotiated


# --------------------------------------------------------------------- batches


@pytest.mark.asyncio
async def test_a_2025_03_26_session_accepts_a_request_batch(app):
    session = await _initialize(app)
    send = await app.call(
        [
            _rpc("ping", 1),
            _rpc("notifications/progress", None),
            _rpc("tools/call", "two", name="recall", arguments={"query": "q"}),
            _rpc("no/such/method", 3),
        ],
        headers=session,
    )

    assert send.status == 200
    assert send.headers[b"content-type"] == b"application/json"
    assert send.headers[b"mcp-session-id"] == session[b"mcp-session-id"]
    responses = send.body
    assert isinstance(responses, list)
    by_id = {r["id"]: r for r in responses}
    assert set(by_id) == {1, "two", 3}  # nothing for the notification
    assert by_id[1]["result"] == {}
    assert by_id["two"]["result"]["content"][0]["text"] == '{"status":"success"}'
    assert by_id[3]["error"]["code"] == -32601
    assert app.executed == ["recall"]


@pytest.mark.asyncio
async def test_a_failing_batch_element_gets_its_own_jsonrpc_error(app, monkeypatch):
    """#1742: an exception while serving one element is that element's
    JSON-RPC error (cause, correlation_id), not a 500 for the whole batch."""
    session = await _initialize(app)

    def broken_tools(_query_string):
        raise RuntimeError("tools/list exploded at /srv/app/x.py")

    monkeypatch.setattr(
        "mcp_server.tools._profiles.select_tool_definitions", broken_tools, raising=True
    )
    send = await app.call([_rpc("ping", 1), _rpc("tools/list", 2)], headers=session)

    assert send.status == 200
    by_id = {r["id"]: r for r in send.body}
    assert by_id[1]["result"] == {}
    error = by_id[2]["error"]
    assert error["code"] == -32603
    assert error["data"]["cause"] == "internal_error"
    assert error["data"]["correlation_id"]
    assert "/srv/app" not in json.dumps(send.body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "batch",
    [
        [_rpc("notifications/initialized", None)],
        [_rpc("notifications/initialized", None), _rpc("notifications/progress", None)],
        [{"jsonrpc": "2.0", "id": 5, "result": {}}],  # a response to a server request
        [_rpc("notifications/progress", None), {"jsonrpc": "2.0", "id": 6, "error": {}}],
    ],
)
async def test_a_notification_or_response_only_batch_is_202(app, batch):
    session = await _initialize(app)
    send = await app.call(batch, headers=session)

    assert send.status == 202
    assert send.raw == b""


@pytest.mark.asyncio
async def test_a_batch_containing_initialize_is_rejected(app):
    session = await _initialize(app)
    send = await app.call(
        [_rpc("ping", 1), _rpc("initialize", 2, protocolVersion="2025-03-26")], headers=session
    )

    assert send.status == 400
    assert send.body["error"]["code"] == -32600
    assert "initialize" in send.body["error"]["message"]
    assert app.executed == []


@pytest.mark.asyncio
async def test_an_empty_batch_is_an_invalid_request(app):
    session = await _initialize(app)
    send = await app.call([], headers=session)

    assert send.status == 400
    assert send.body["error"]["code"] == -32600
    assert send.body["id"] is None


@pytest.mark.asyncio
async def test_invalid_elements_get_their_own_error_in_the_batch(app):
    session = await _initialize(app)
    send = await app.call([1, {"jsonrpc": "2.0", "id": 7}, _rpc("ping", 8)], headers=session)

    assert send.status == 200
    responses = send.body
    assert len(responses) == 3
    assert [r["error"]["code"] for r in responses[:2]] == [-32600, -32600]
    assert responses[0]["id"] is None
    assert responses[1]["id"] == 7
    assert responses[2] == {"jsonrpc": "2.0", "id": 8, "result": {}}


@pytest.mark.asyncio
async def test_an_oversized_batch_is_rejected_before_anything_runs(app):
    session = await _initialize(app)
    batch = [
        _rpc("tools/call", i, name="recall", arguments={})
        for i in range(transport.MAX_BATCH_MESSAGES + 1)
    ]
    send = await app.call(batch, headers=session)

    assert send.status == 400
    assert send.body["error"]["code"] == -32600
    assert app.executed == []


@pytest.mark.asyncio
async def test_a_2024_11_05_session_keeps_refusing_batches(app):
    session = await _initialize(app, "2024-11-05")
    send = await app.call([_rpc("ping", 1)], headers=session)

    assert send.status == 400
    assert send.body["error"]["code"] == -32600
    assert "batch" in send.body["error"]["message"].lower()


@pytest.mark.asyncio
async def test_a_re_adopted_session_of_unknown_version_refuses_batches(app):
    """The negotiated version lives in memory; after a restart it is unknown,
    and a batch is only accepted where the revision is known to allow it."""
    send = await app.call([_rpc("ping", 1)], headers={b"mcp-session-id": b"mcp-0123456789abcdef"})
    assert send.status == 400
    assert send.body["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_a_scope_refusal_inside_a_batch_carries_the_challenge(app, monkeypatch):
    """One element refused for scope: the others still run, and the batch is
    answered 403 with the ``insufficient_scope`` challenge so the client can
    step up."""
    session = await _initialize(app)
    monkeypatch.setattr(transport, "get_mcp_oauth_scopes", lambda: frozenset({"memory:read"}))
    send = await app.call(
        [
            _rpc("tools/call", 1, name="recall", arguments={}),
            _rpc("tools/call", 2, name="remember", arguments={}),
        ],
        headers=session,
    )

    assert send.status == 403
    assert b"insufficient_scope" in send.headers[b"www-authenticate"]
    by_id = {r["id"]: r for r in send.body}
    assert "result" in by_id[1]
    assert by_id[2]["error"]["data"]["error"] == "insufficient_scope"
    assert app.executed == ["recall"]
