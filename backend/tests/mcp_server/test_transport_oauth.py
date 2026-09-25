"""OAuth access tokens on ``/mcp``: audience, challenges, scope and sessions (#1686).

* Audience (RFC 8707): a token bound to a resource must be bound to this
  server's MCP resource — what ``/.well-known/oauth-protected-resource``
  publishes, ``/mcp`` and ``/mcp/`` alike. A token without one is accepted.
* 401 challenges (RFC 6750 §3.1): ``error="invalid_token"`` for an invalid,
  expired, revoked or other-audience token; no error code without credentials;
  ``resource_metadata`` on every one.
* ``tools/call`` scope (``mcp_server.tools._scopes``) on both eras: HTTP 403
  with an ``insufficient_scope`` challenge and a JSON-RPC error in the
  tool-error vocabulary. OAuth tokens only; ``initialize`` / ``tools/list`` /
  ``ping`` / ``server/discover`` are not gated. A token whose stored scope
  names no ``memory:*`` scope gets its client's registered ``memory:*``
  scopes, else the DCR default.
* Sessions on ``/mcp`` and ``/mcp/``: an unknown ``Mcp-Session-Id`` is
  re-adopted for the caller, another user's or workspace's session is 404,
  ``DELETE`` ends the caller's own session (204). Other methods and paths
  never open a session.

``mcp_asgi_app`` runs with the real ``authenticate_mcp_request``; only the
token lookups, the workspace lookup, the session store and the tool dispatch
are stubbed.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

import mcp_server.auth as mcp_auth
import mcp_server.tools as tools_mod
import mcp_server.transport as transport
from auth.mcp_scopes import DCR_DEFAULT_SCOPE
from mcp_server.auth import (
    MissingCredentialsError,
    OAuthGrant,
    authenticate_mcp_request,
    get_mcp_oauth_scopes,
    mcp_resource_url,
)
from mcp_server.transport import mcp_asgi_app
from utils.exceptions import InvalidTokenError

ORIGIN = "https://memory.example.com"
MODERN = "2026-07-28"
PV_KEY = "io.modelcontextprotocol/protocolVersion"


@pytest.fixture(autouse=True)
def _origin(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", ORIGIN)
    monkeypatch.delenv("MCP_BASE_PATH", raising=False)


def _parse_challenge(value: bytes | None) -> dict[str, str]:
    """``Bearer a="x", b="y"`` → ``{"scheme": "Bearer", "a": "x", "b": "y"}``."""
    assert value is not None, "no WWW-Authenticate header"
    scheme, _, rest = value.decode().partition(" ")
    out = {"scheme": scheme}
    for part in rest.split('", '):
        name, _, raw = part.partition("=")
        out[name.strip()] = raw.strip().strip('"')
    return out


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

    @property
    def challenge(self) -> dict[str, str]:
        return _parse_challenge(self.headers.get(b"www-authenticate"))


class _Sessions:
    """Session store double: records calls, holds sessions by id."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._sessions: dict[str, SimpleNamespace] = {}

    def add(self, session_id: str, user_id: str = "user-1", workspace_id=None) -> None:
        self._sessions[session_id] = SimpleNamespace(
            session_id=session_id, user_id=user_id, workspace_id=workspace_id
        )

    async def get_or_create_session(self, user_id, workspace_id=None, session_id=None):
        self.calls.append(("get_or_create_session", session_id))
        session_id = session_id or "mcp-server-minted"
        self.add(session_id, user_id, workspace_id)
        return self._sessions[session_id]

    async def get_session(self, session_id):
        self.calls.append(("get_session", session_id))
        return self._sessions.get(session_id)

    async def remove_session(self, session_id):
        self.calls.append(("remove_session", session_id))
        self._sessions.pop(session_id, None)


@pytest.fixture
def app(monkeypatch):
    """``mcp_asgi_app`` whose Bearer token resolves to ``app.grant`` (an OAuth
    grant) or, when ``app.api_key`` is set, to an API key."""
    state = SimpleNamespace(
        grant=OAuthGrant("user-1", "memory:read memory:write", f"{ORIGIN}/mcp"),
        api_key=False,
        sessions=_Sessions(),
        executed=[],
    )

    async def verify_api_key(_token):
        return ("user-1", None, None) if state.api_key else None

    async def verify_oauth2_token(_token):
        return state.grant

    async def no_workspace(_user_id):
        return None

    async def execute_tool_call(**kwargs):
        state.executed.append(kwargs["tool_name"])
        return [SimpleNamespace(type="text", text='{"status":"success"}')]

    monkeypatch.setattr(mcp_auth, "_verify_api_key", verify_api_key)
    monkeypatch.setattr(mcp_auth, "_verify_oauth2_token", verify_oauth2_token)
    monkeypatch.setattr(transport, "_get_user_workspace_id", no_workspace)
    monkeypatch.setattr(transport, "get_session_manager", lambda: state.sessions)
    monkeypatch.setattr(tools_mod, "execute_tool_call", execute_tool_call)

    async def call(
        body: dict | None,
        *,
        headers: dict[bytes, bytes] | None = None,
        path: str = "/mcp/",
        method: str = "POST",
        token: str | None = "opaque-oauth-token",
    ) -> _Recorder:
        request_headers = dict(headers or {})
        if token is not None:
            request_headers[b"authorization"] = f"Bearer {token}".encode()
        raw = json.dumps(body).encode() if body is not None else b""

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        send = _Recorder()
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": list(request_headers.items()),
        }
        await mcp_asgi_app(scope, receive, send)
        return send

    state.call = call
    return state


def _rpc(method: str, request_id: int = 1, **params) -> dict:
    body: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        body["params"] = params
    return body


def _modern(method: str, request_id: int = 1, **params) -> tuple[dict, dict[bytes, bytes]]:
    params["_meta"] = {PV_KEY: MODERN, "io.modelcontextprotocol/clientCapabilities": {}}
    body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    headers = {b"mcp-protocol-version": MODERN.encode(), b"mcp-method": method.encode()}
    if "name" in params:
        headers[b"mcp-name"] = params["name"].encode()
    return body, headers


# ------------------------------------------------------------------- audience


@pytest.mark.asyncio
async def test_the_mcp_resource_is_what_the_well_known_document_publishes():
    from api.routes.well_known import oauth_protected_resource

    published = (await oauth_protected_resource())["resource"]
    assert mcp_resource_url() == published == f"{ORIGIN}/mcp"


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", [None, "", f"{ORIGIN}/mcp", f"{ORIGIN}/mcp/"])
async def test_a_token_for_this_resource_or_without_one_is_accepted(monkeypatch, resource):
    grant = OAuthGrant("user-1", "memory:read", resource)
    monkeypatch.setattr(mcp_auth, "_verify_api_key", _none)
    monkeypatch.setattr(mcp_auth, "_verify_oauth2_token", _returning(grant))

    assert await authenticate_mcp_request("Bearer tok") == ("user-1", None, None)
    assert get_mcp_oauth_scopes() == {"memory:read"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource",
    [
        "https://other.example.com/mcp",
        f"{ORIGIN}/api/v1",
        f"{ORIGIN}/mcp/w/0b7f",
        f"{ORIGIN}/mcpx",
        "http://memory.example.com/mcp",
    ],
)
async def test_a_token_for_another_resource_is_an_invalid_token(monkeypatch, resource):
    grant = OAuthGrant("user-1", "memory:read memory:write", resource)
    monkeypatch.setattr(mcp_auth, "_verify_api_key", _none)
    monkeypatch.setattr(mcp_auth, "_verify_oauth2_token", _returning(grant))

    with pytest.raises(InvalidTokenError, match="different resource"):
        await authenticate_mcp_request("Bearer tok")
    assert get_mcp_oauth_scopes() is None


@pytest.mark.asyncio
async def test_other_audience_is_a_401_invalid_token_on_the_wire(app):
    app.grant = OAuthGrant("user-1", "memory:read memory:write", "https://other.example.com/mcp")
    send = await app.call(_rpc("initialize"))

    assert send.status == 401
    assert send.challenge["error"] == "invalid_token"
    assert send.challenge["resource_metadata"].endswith("/.well-known/oauth-protected-resource")
    assert app.sessions.calls == []


# --------------------------------------------------------------- 401 challenges


@pytest.mark.asyncio
async def test_no_credentials_get_a_challenge_without_an_error_code(app):
    send = await app.call(_rpc("initialize"), token=None)

    assert send.status == 401
    challenge = send.challenge
    assert challenge["scheme"] == "Bearer"
    assert "error" not in challenge
    assert "error_description" not in challenge
    assert challenge["resource_metadata"].endswith("/.well-known/oauth-protected-resource")


@pytest.mark.asyncio
async def test_an_unknown_expired_or_revoked_token_is_invalid_token(app):
    """The lookup answers ``None`` for all three (``find_active_oauth_token``)."""
    app.grant = None
    send = await app.call(_rpc("initialize"))

    assert send.status == 401
    assert send.challenge["error"] == "invalid_token"
    assert send.challenge["resource_metadata"]
    assert send.body["error"] == "invalid_token"


@pytest.mark.asyncio
async def test_a_malformed_authorization_header_stays_invalid_request(app):
    send = await app.call(_rpc("initialize"), headers={b"authorization": b"Basic abc"}, token=None)

    assert send.status == 401
    assert send.challenge["error"] == "invalid_request"
    assert send.challenge["resource_metadata"]


@pytest.mark.asyncio
async def test_missing_and_invalid_credentials_raise_distinct_errors(monkeypatch):
    monkeypatch.setattr(mcp_auth, "_verify_api_key", _none)
    monkeypatch.setattr(mcp_auth, "_verify_oauth2_token", _none)
    monkeypatch.setattr(mcp_auth, "_verify_session_cookie", _none)

    with pytest.raises(MissingCredentialsError):
        await authenticate_mcp_request(None)
    with pytest.raises(MissingCredentialsError):
        await authenticate_mcp_request(None, cookie_header=b"kagura_session=stale")
    with pytest.raises(InvalidTokenError):
        await authenticate_mcp_request("Bearer not-a-token")


# ------------------------------------------------------------ granted scopes


@pytest.mark.parametrize(
    ("stored", "client", "expected"),
    [
        ("memory:read", "memory:read memory:write", {"memory:read"}),
        ("memory:read offline_access", None, {"memory:read", "offline_access"}),
        ("memory:read,memory:write", None, {"memory:read", "memory:write"}),
        # No memory:* scope on the token: the client's registered memory:* scopes.
        (
            "claudeai",
            "openid memory:read memory:write",
            {"claudeai", "memory:read", "memory:write"},
        ),
        ("openid offline_access", "memory:read", {"openid", "offline_access", "memory:read"}),
        (None, "memory:read,memory:write", {"memory:read", "memory:write"}),
        ("", "memory:write", {"memory:write"}),
    ],
)
def test_effective_scopes(stored, client, expected):
    assert mcp_auth.granted_scopes(stored, client) == expected


@pytest.mark.parametrize("stored", [None, "", "   ", "claudeai", "openid offline_access"])
@pytest.mark.parametrize("client", [None, "", "openid offline_access"])
def test_without_memory_scopes_anywhere_the_dcr_default_applies(caplog, stored, client):
    with caplog.at_level(logging.DEBUG, logger="mcp_server.auth"):
        granted = mcp_auth.granted_scopes(stored, client)

    dcr_memory = {s for s in DCR_DEFAULT_SCOPE.split() if s.startswith("memory:")}
    assert {s for s in granted if s.startswith("memory:")} == dcr_memory
    fallbacks = [r for r in caplog.records if "names no memory scope" in r.getMessage()]
    assert len(fallbacks) == 1
    assert fallbacks[0].levelno == logging.DEBUG
    assert "DCR default" in fallbacks[0].getMessage()


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "looked_up"),
    [("memory:read", False), ("claudeai", True), ("", True), (None, True)],
)
async def test_the_client_scope_is_read_only_when_the_token_names_no_memory_scope(
    monkeypatch, stored, looked_up
):
    import auth.oauth2_bearer as bearer
    import db.base

    row = SimpleNamespace(user_id="u-1", scope=stored, resource=None, client_id="client-1")
    queries: list = []

    class _Db:
        async def execute(self, stmt):
            queries.append(stmt)
            return _ScalarResult("memory:read")

    async def get_db():
        yield _Db()

    async def find(_token, _db):
        return row

    monkeypatch.setattr(db.base, "get_db", get_db)
    monkeypatch.setattr(bearer, "find_active_oauth_token", find)

    grant = await mcp_auth._verify_oauth2_token("tok")

    assert grant.user_id == "u-1"
    assert bool(queries) is looked_up
    assert grant.client_scope == ("memory:read" if looked_up else None)


@pytest.mark.asyncio
async def test_api_keys_carry_no_oauth_scope(monkeypatch):
    """A previous OAuth request's scopes never leak into an API-key request."""
    monkeypatch.setattr(
        mcp_auth, "_verify_oauth2_token", _returning(OAuthGrant("u", "memory:read", None))
    )
    monkeypatch.setattr(mcp_auth, "_verify_api_key", _none)
    await authenticate_mcp_request("Bearer oauth")
    assert get_mcp_oauth_scopes() == {"memory:read"}

    monkeypatch.setattr(mcp_auth, "_verify_api_key", _returning(("u", None, None)))
    await authenticate_mcp_request("Bearer kagura_key")
    assert get_mcp_oauth_scopes() is None


# ------------------------------------------------------ tools/call scope, legacy


async def _open_session(app) -> None:
    app.sessions.add("mcp-open")


async def _initialize(app) -> dict[bytes, bytes]:
    """A real ``initialize``: the session the rest of the calls ride on."""
    opened = await app.call(_rpc("initialize", protocolVersion="2025-03-26"))
    assert opened.status == 200
    return {b"mcp-session-id": opened.headers[b"mcp-session-id"]}


@pytest.mark.asyncio
async def test_legacy_write_call_without_write_scope_is_403_insufficient_scope(app):
    app.grant = OAuthGrant("user-1", "memory:read offline_access", None)
    await _open_session(app)
    send = await app.call(
        _rpc("tools/call", 9, name="remember", arguments={"context_id": "c", "summary": "s"}),
        headers={b"mcp-session-id": b"mcp-open"},
    )

    assert send.status == 403
    challenge = send.challenge
    assert challenge["error"] == "insufficient_scope"
    # What is granted stays in the challenge (a client re-authorizes with it).
    assert challenge["scope"] == "memory:read memory:write offline_access"
    assert challenge["resource_metadata"].endswith("/.well-known/oauth-protected-resource")

    body = send.body
    assert body["id"] == 9
    assert body["error"]["code"] == -32002
    data = body["error"]["data"]
    assert data["error"] == "insufficient_scope"
    assert data["required_scope"] == "memory:write"
    assert "memory:write" in data["help"] and "Reconnect" in data["help"]
    assert "remember" in body["error"]["message"]
    assert app.executed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_contexts", "recall", "get_agent_bootstrap"])
async def test_legacy_read_calls_run_with_a_read_only_token(app, tool):
    app.grant = OAuthGrant("user-1", "memory:read", None)
    await _open_session(app)
    send = await app.call(
        _rpc("tools/call", 3, name=tool, arguments={}), headers={b"mcp-session-id": b"mcp-open"}
    )

    assert send.status == 200
    assert "result" in send.body
    assert app.executed == [tool]


@pytest.mark.asyncio
async def test_a_write_only_token_cannot_read(app):
    app.grant = OAuthGrant("user-1", "memory:write", None)
    await _open_session(app)
    send = await app.call(
        _rpc("tools/call", 3, name="list_contexts"), headers={b"mcp-session-id": b"mcp-open"}
    )

    assert send.status == 403
    assert send.body["error"]["data"]["required_scope"] == "memory:read"
    assert send.challenge["scope"] == "memory:read memory:write"


@pytest.mark.asyncio
async def test_handshake_listing_and_ping_are_not_scope_gated(app):
    app.grant = OAuthGrant("user-1", "memory:read", None)
    session = await _initialize(app)

    assert (await app.call(_rpc("tools/list", 2), headers=session)).status == 200
    assert (await app.call(_rpc("ping", 3), headers=session)).status == 200
    call = await app.call(_rpc("tools/call", 4, name="forget", arguments={}), headers=session)
    assert call.status == 403


@pytest.mark.asyncio
async def test_the_scope_comes_from_the_request_token_not_the_session(app):
    """A session opened with a read-write token does not lend its scope."""
    app.grant = OAuthGrant("user-1", "memory:read memory:write", None)
    session = await _initialize(app)

    app.grant = OAuthGrant("user-1", "memory:read", None)
    send = await app.call(
        _rpc("tools/call", 3, name="forget", arguments={"context_id": "c", "memory_id": "m"}),
        headers=session,
    )
    assert send.status == 403
    assert app.executed == []


@pytest.mark.asyncio
async def test_a_session_opened_read_only_serves_a_read_write_token(app):
    """…and a session opened with a read-only token does not withhold it."""
    app.grant = OAuthGrant("user-1", "memory:read", None)
    session = await _initialize(app)

    app.grant = OAuthGrant("user-1", "memory:read memory:write", None)
    send = await app.call(_rpc("tools/call", 3, name="remember", arguments={}), headers=session)
    assert send.status == 200
    assert app.executed == ["remember"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential", ["api_key", "unscoped_oauth", "client_specific_scope", "comma_separated"]
)
async def test_api_keys_and_tokens_without_memory_scopes_can_write(app, credential):
    if credential == "api_key":
        app.api_key = True
    elif credential == "unscoped_oauth":
        app.grant = OAuthGrant("user-1", None, None)  # the DCR default applies
    elif credential == "client_specific_scope":
        app.grant = OAuthGrant("user-1", "claudeai", None, "memory:read memory:write")
    else:
        app.grant = OAuthGrant("user-1", "memory:read,memory:write", None)
    await _open_session(app)
    send = await app.call(
        _rpc("tools/call", 3, name="remember", arguments={}),
        headers={b"mcp-session-id": b"mcp-open"},
    )

    assert send.status == 200
    assert app.executed == ["remember"]


@pytest.mark.asyncio
async def test_a_client_registered_read_only_limits_a_token_without_memory_scopes(app):
    app.grant = OAuthGrant("user-1", "openid offline_access", None, "openid memory:read")
    await _open_session(app)
    headers = {b"mcp-session-id": b"mcp-open"}

    assert (
        await app.call(_rpc("tools/call", 3, name="list_contexts"), headers=headers)
    ).status == 200
    denied = await app.call(_rpc("tools/call", 4, name="remember", arguments={}), headers=headers)
    assert denied.status == 403
    assert denied.challenge["scope"] == "openid memory:read memory:write offline_access"


# ---------------------------------------------------- tools/call scope, stateless


@pytest.mark.asyncio
async def test_stateless_write_call_without_write_scope_is_403_insufficient_scope(app):
    app.grant = OAuthGrant("user-1", "memory:read", None)
    body, headers = _modern("tools/call", 5, name="remember", arguments={})
    send = await app.call(body, headers=headers)

    assert send.status == 403
    assert send.challenge["error"] == "insufficient_scope"
    assert send.challenge["scope"] == "memory:read memory:write"
    assert send.body["error"]["code"] == -32603
    assert send.body["error"]["data"]["error"] == "insufficient_scope"
    assert b"mcp-session-id" not in send.headers
    assert app.executed == []
    assert app.sessions.calls == []


@pytest.mark.asyncio
async def test_stateless_read_only_token_lists_discovers_and_reads(app):
    app.grant = OAuthGrant("user-1", "memory:read", None)

    for method in ("tools/list", "server/discover"):
        body, headers = _modern(method, 6)
        send = await app.call(body, headers=headers)
        assert send.status == 200, method
        assert "result" in send.body, method

    body, headers = _modern("tools/call", 5, name="recall", arguments={"query": "q"})
    send = await app.call(body, headers=headers)
    assert send.status == 200
    assert app.executed == ["recall"]


# ------------------------------------------------------------------ sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/mcp/", "/mcp"])
@pytest.mark.parametrize("method", ["POST", "GET"])
async def test_an_unknown_session_id_is_re_adopted_for_the_caller(app, path, method):
    """A client that cannot re-initialize after a 404 keeps working across a
    restart, a deploy or the idle timeout."""
    body = _rpc("ping", 7) if method == "POST" else None
    send = await app.call(body, headers={b"mcp-session-id": b"mcp-gone"}, path=path, method=method)

    assert send.status == 200
    assert send.headers[b"mcp-session-id"] == b"mcp-gone"
    assert ("get_or_create_session", "mcp-gone") in app.sessions.calls
    adopted = app.sessions._sessions["mcp-gone"]
    assert (adopted.user_id, adopted.workspace_id) == ("user-1", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
@pytest.mark.parametrize("owner", [("user-2", None), ("user-1", "ws-other")])
async def test_another_users_or_workspaces_session_is_404(app, owner, method):
    app.sessions.add("mcp-theirs", *owner)
    body = _rpc("ping", 7) if method == "POST" else None
    send = await app.call(body, headers={b"mcp-session-id": b"mcp-theirs"}, method=method)

    assert send.status == 404
    assert "re-initialize" in send.body["error"]["message"]
    assert "initialize" in send.body["error"]["data"]["action"]
    assert "mcp-theirs" in app.sessions._sessions  # untouched
    assert not any(call[0] == "get_or_create_session" for call in app.sessions.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/mcp/", "/mcp"])
async def test_delete_ends_the_callers_own_session(app, path):
    await _open_session(app)
    send = await app.call(
        None, headers={b"mcp-session-id": b"mcp-open"}, path=path, method="DELETE"
    )

    assert send.status == 204
    assert "mcp-open" not in app.sessions._sessions


@pytest.mark.asyncio
async def test_delete_of_an_unknown_session_is_404_and_creates_nothing(app):
    send = await app.call(None, headers={b"mcp-session-id": b"mcp-gone"}, method="DELETE")

    assert send.status == 404
    assert "mcp-gone" not in app.sessions._sessions


@pytest.mark.asyncio
async def test_delete_without_a_session_id_is_400(app):
    send = await app.call(None, method="DELETE")
    assert send.status == 400
    assert app.sessions.calls == []


@pytest.mark.asyncio
async def test_a_known_session_is_used(app):
    await _open_session(app)
    send = await app.call(_rpc("ping", 7), headers={b"mcp-session-id": b"mcp-open"})

    assert send.status == 200
    assert send.headers[b"mcp-session-id"] == b"mcp-open"


@pytest.mark.asyncio
async def test_initialize_without_a_session_id_gets_a_server_minted_one(app):
    send = await app.call(_rpc("initialize", protocolVersion="2025-03-26"))

    assert send.status == 200
    assert send.headers[b"mcp-session-id"] == b"mcp-server-minted"
    assert app.sessions.calls == [("get_or_create_session", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("POST", "/mcp/elsewhere", 404),
        ("GET", "/mcp/elsewhere", 404),
        ("PUT", "/mcp/", 405),
        ("PATCH", "/mcp", 405),
        ("GET", "/mcp/sse", 410),
    ],
)
async def test_other_methods_and_paths_open_no_session(app, method, path, status):
    send = await app.call(
        _rpc("ping", 7), headers={b"mcp-session-id": b"mcp-chosen"}, path=path, method=method
    )

    assert send.status == status
    if status == 405:
        assert send.headers[b"allow"] == b"GET, POST, DELETE"
    assert app.sessions.calls == []


# ------------------------------------------------------------------ logging


@pytest.mark.asyncio
async def test_debug_logging_never_records_credential_values(app, caplog):
    cookie = b"kagura_session=cookie-value-sentinel"
    with caplog.at_level(logging.DEBUG):
        await app.call(
            _rpc("initialize", protocolVersion="2025-03-26"),
            headers={b"cookie": cookie},
            token="bearer-value-sentinel",
        )

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "MCP headers" in logged  # the debug lines did run
    assert "bearer-value-sentinel" not in logged
    assert "cookie-value-sentinel" not in logged


# -------------------------------------------------------------------- helpers


async def _none(*_args, **_kwargs):
    return None


def _returning(value):
    async def _fn(*_args, **_kwargs):
        return value

    return _fn
