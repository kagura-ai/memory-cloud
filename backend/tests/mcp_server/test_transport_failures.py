"""Transport-level failures are actionable (#1742).

* Authentication: a failed token lookup (database down) is HTTP 503
  ``temporarily_unavailable`` with ``Retry-After`` and a ``correlation_id`` —
  no ``WWW-Authenticate`` challenge (it would make the client re-authorize) and
  no exception text. A bad token is still a 401 challenge. The OAuth
  ``/mcp/w/{id}`` membership query is guarded the same way.
* An exception outside tool dispatch (opening a session, ``initialize``, the
  stateless handler) is a JSON-RPC error carrying ``cause``,
  ``correlation_id`` and ``help`` instead of a bare HTTP 500; it is re-raised
  only when the response has already started.
* A non-object ``arguments`` is ``-32602`` on both eras.

``mcp_asgi_app`` runs with the real ``authenticate_mcp_request``; the token
lookups, the workspace lookup and the session store are stubbed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

import auth.oauth2_bearer as bearer
import mcp_server.auth as mcp_auth
import mcp_server.tools as tools_mod
import mcp_server.tools._errors as errors_mod
import mcp_server.transport as transport
from mcp_server.auth import OAuthGrant
from mcp_server.transport import mcp_asgi_app

ORIGIN = "https://memory.example.com"
MODERN = "2026-07-28"
PV_KEY = "io.modelcontextprotocol/protocolVersion"
# A server-minted session id shape (#1740): only these are re-adopted.
SESSION_ID = b"mcp-0123456789abcdef"
DB_DOWN = ConnectionRefusedError("[Errno 111] Connect call failed ('127.0.0.1', 5432)")


@pytest.fixture(autouse=True)
def _origin(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", ORIGIN)
    monkeypatch.delenv("MCP_BASE_PATH", raising=False)


# The correlation id is random hex, so a digits-only needle ("5432", "6379")
# can occur in it by chance (#1810). Pin one that contains both ports: a no-leak
# assertion that searches for a digit string now fails every run, not 1 in ~1000.
COLLIDING_CORRELATION_ID = "0005432000637900"


@pytest.fixture(autouse=True)
def _colliding_correlation_id(monkeypatch):
    monkeypatch.setattr(errors_mod, "new_correlation_id", lambda: COLLIDING_CORRELATION_ID)


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


class _Sessions:
    def __init__(self) -> None:
        self.fail_with: BaseException | None = None
        self.lookup_fails_with: BaseException | None = None

    async def get_or_create_session(self, user_id, workspace_id=None, session_id=None):
        if self.fail_with is not None:
            raise self.fail_with
        return SimpleNamespace(
            session_id=session_id or "sess-minted", user_id=user_id, workspace_id=workspace_id
        )

    async def get_owned_session(self, session_id, user_id, workspace_id):
        if self.lookup_fails_with is not None:
            raise self.lookup_fails_with
        return "missing", None


@pytest.fixture
def app(monkeypatch):
    state = SimpleNamespace(
        grant=OAuthGrant("user-1", "memory:read memory:write", f"{ORIGIN}/mcp"),
        oauth_error=None,
        sessions=_Sessions(),
    )

    async def verify_api_key(_token):
        return None

    async def verify_oauth2_token(_token):
        if state.oauth_error is not None:
            raise state.oauth_error
        return state.grant

    async def no_workspace(_user_id):
        return None

    async def execute_tool_call(**kwargs):
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
        scope_extra: dict | None = None,
    ) -> _Recorder:
        request_headers = {b"authorization": b"Bearer opaque-oauth-token", **(headers or {})}
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
            **(scope_extra or {}),
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


def _assert_no_leak(send: _Recorder) -> None:
    # Needles that cannot occur in a hex correlation id (#1810): the host and
    # the asyncpg wording, not the bare port number.
    raw = b"".join(m.get("body", b"") for m in send.messages[1:]).decode()
    assert "127.0.0.1" not in raw
    assert "Connect call failed" not in raw
    assert "Errno" not in raw
    assert "Traceback" not in raw


# ------------------------------------------------------------ authentication


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        DB_DOWN,
        OperationalError("SELECT 1", {}, Exception("server closed the connection")),
        OSError(),
    ],
)
async def test_a_failed_token_lookup_is_503_not_401(app, error):
    app.oauth_error = error
    send = await app.call(_rpc("initialize"))

    assert send.status == 503
    assert b"www-authenticate" not in send.headers
    assert send.headers[b"retry-after"] == b"5"
    assert send.body["error"] == "temporarily_unavailable"
    assert send.body["error_description"] == (
        "Could not verify credentials right now; retry shortly."
    )
    assert send.body["correlation_id"]
    _assert_no_leak(send)


@pytest.mark.asyncio
async def test_the_503_is_logged_with_the_correlation_id(app, caplog):
    app.oauth_error = DB_DOWN
    with caplog.at_level("ERROR", logger="mcp_server.transport"):
        send = await app.call(_rpc("initialize"))

    record = next(r for r in caplog.records if "MCP auth unavailable" in r.getMessage())
    assert send.body["correlation_id"] in record.getMessage()
    assert record.exc_info is not None


@pytest.mark.asyncio
async def test_a_bad_token_is_still_a_401_challenge(app):
    app.grant = None
    send = await app.call(_rpc("initialize"))

    assert send.status == 401
    assert b"www-authenticate" in send.headers
    assert send.body["error"] == "invalid_token"


@pytest.mark.asyncio
async def test_the_modern_era_gets_the_same_503(app):
    app.oauth_error = DB_DOWN
    body, headers = _modern("server/discover")
    send = await app.call(body, headers=headers)

    assert send.status == 503
    assert b"www-authenticate" not in send.headers


@pytest.mark.asyncio
async def test_the_mcp_token_lookup_raises_on_a_database_error():
    """``find_active_oauth_token`` answers ``None`` on a lookup failure for REST
    (unchanged); MCP asks it to raise, including asyncpg's bare OSError."""

    class _FailingDb:
        async def execute(self, _stmt):
            raise DB_DOWN

    with pytest.raises(ConnectionRefusedError):
        await bearer.find_active_oauth_token("tok", _FailingDb(), raise_on_lookup_error=True)

    class _SqlFailingDb:
        async def execute(self, _stmt):
            raise OperationalError("SELECT 1", {}, Exception("down"))

    assert await bearer.find_active_oauth_token("tok", _SqlFailingDb()) is None


@pytest.mark.asyncio
async def test_the_mcp_verifier_passes_raise_on_lookup_error(monkeypatch):
    seen: dict = {}

    async def find(_token, _db, **kwargs):
        seen.update(kwargs)
        raise DB_DOWN

    async def get_db():
        yield object()

    monkeypatch.setattr(bearer, "find_active_oauth_token", find)
    monkeypatch.setattr("db.base.get_db", get_db)
    with pytest.raises(ConnectionRefusedError):
        await mcp_auth._verify_oauth2_token("tok")
    assert seen == {"raise_on_lookup_error": True}


@pytest.mark.asyncio
async def test_a_failed_membership_query_is_503(app, monkeypatch):
    class _FailingDb:
        async def execute(self, _stmt):
            raise DB_DOWN

    async def get_db():
        yield _FailingDb()

    monkeypatch.setattr("db.base.get_db", get_db)
    send = await app.call(_rpc("initialize"), scope_extra={"workspace_id_from_url": str(uuid4())})

    assert send.status == 503
    assert send.body["error"] == "temporarily_unavailable"
    _assert_no_leak(send)


@pytest.mark.asyncio
async def test_a_malformed_workspace_id_in_the_url_is_still_400(app):
    send = await app.call(_rpc("initialize"), scope_extra={"workspace_id_from_url": "not-a-uuid"})

    assert send.status == 400
    assert send.body["error"] == "invalid_request"


# ------------------------------------------------------- transport failures


def _assert_jsonrpc_failure(send: _Recorder, *, request_id, status: int, cause: str) -> dict:
    assert send.status == status
    assert send.headers[b"content-type"] == b"application/json"
    body = send.body
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == request_id
    error = body["error"]
    assert error["code"] == -32603
    data = error["data"]
    assert data["cause"] == cause
    assert data["correlation_id"]
    assert data["help"]
    _assert_no_leak(send)
    return data


@pytest.mark.asyncio
async def test_a_session_creation_failure_is_a_jsonrpc_error(app):
    app.sessions.fail_with = DB_DOWN
    send = await app.call(_rpc("initialize", request_id=7))

    data = _assert_jsonrpc_failure(send, request_id=7, status=503, cause="service_unavailable")
    assert send.headers[b"retry-after"] == b"5"
    assert data["retryable"] is True
    assert data["retry_after_seconds"] == 5


@pytest.mark.asyncio
async def test_a_session_re_adoption_failure_is_a_jsonrpc_error(app):
    app.sessions.fail_with = RuntimeError("boom at /srv/app/session.py")
    send = await app.call(_rpc("tools/list", request_id=3), headers={b"mcp-session-id": SESSION_ID})

    data = _assert_jsonrpc_failure(send, request_id=3, status=500, cause="internal_error")
    assert "boom" not in json.dumps(send.body)
    assert "retry_after_seconds" not in data


@pytest.mark.asyncio
async def test_a_session_lookup_failure_is_a_jsonrpc_error(app):
    app.sessions.lookup_fails_with = ConnectionError("redis at 10.0.0.9:6379 refused")
    send = await app.call(_rpc("tools/list", request_id=4), headers={b"mcp-session-id": SESSION_ID})

    _assert_jsonrpc_failure(send, request_id=4, status=503, cause="service_unavailable")
    body = json.dumps(send.body)
    assert "10.0.0.9" not in body
    assert "refused" not in body


@pytest.mark.asyncio
async def test_a_timeout_uses_the_legacy_timeout_code(app):
    app.sessions.lookup_fails_with = TimeoutError()
    send = await app.call(_rpc("tools/list", request_id=4), headers={b"mcp-session-id": SESSION_ID})

    assert send.status == 503
    assert send.body["error"]["code"] == -32001
    assert send.body["error"]["data"]["cause"] == "timeout"


@pytest.mark.asyncio
async def test_a_legacy_handler_failure_is_a_jsonrpc_error(app, monkeypatch):
    async def broken_instructions(**_kwargs):
        raise RuntimeError("instructions exploded at /srv/app/x.py")

    monkeypatch.setattr(transport, "build_instructions", broken_instructions)
    send = await app.call(_rpc("initialize", request_id=11))

    _assert_jsonrpc_failure(send, request_id=11, status=500, cause="internal_error")
    assert send.headers[b"mcp-session-id"] == b"sess-minted"


@pytest.mark.asyncio
async def test_a_stateless_handler_failure_is_a_jsonrpc_error(app, monkeypatch):
    import mcp_server.transport_stateless as stateless

    async def broken_instructions(**_kwargs):
        raise DB_DOWN

    monkeypatch.setattr(stateless, "build_instructions", broken_instructions)
    body, headers = _modern("server/discover", request_id=5)
    send = await app.call(body, headers=headers)

    _assert_jsonrpc_failure(send, request_id=5, status=503, cause="service_unavailable")


@pytest.mark.asyncio
async def test_a_write_with_an_unknown_outcome_gets_no_retry_after(app, monkeypatch):
    """A tools/call failing in the transport keeps the tool's advice: a write
    may have run, so there is no Retry-After inviting an automatic repeat."""

    async def broken_post(scope, receive, send, session, headers):
        raise DB_DOWN

    monkeypatch.setattr(transport, "handle_streamable_http_post", broken_post)
    send = await app.call(
        _rpc("tools/call", request_id=9, name="remember", arguments={}),
    )

    assert send.status == 503
    assert b"retry-after" not in send.headers
    data = send.body["error"]["data"]
    assert data["outcome"] == "unknown"
    assert data["retryable"] is False


@pytest.mark.asyncio
async def test_a_failure_after_the_response_started_is_re_raised(app, monkeypatch):
    async def half_sent(scope, receive, send, session, headers):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("stream broke")

    monkeypatch.setattr(transport, "handle_streamable_http_post", half_sent)
    with pytest.raises(RuntimeError, match="stream broke"):
        await app.call(_rpc("initialize"))


@pytest.mark.asyncio
async def test_the_transport_failure_is_logged_with_the_correlation_id(app):
    exc = RuntimeError("boom")
    app.sessions.fail_with = exc
    with patch("mcp_server.tools._errors.logger") as log:
        send = await app.call(_rpc("initialize"))

    event = log.error.call_args
    assert event.args == ("mcp_transport_failed",)
    assert event.kwargs["correlation_id"] == send.body["error"]["data"]["correlation_id"]
    assert event.kwargs["exc_info"] is exc
    assert event.kwargs["method"] == "initialize"


# ------------------------------------------------------ non-object arguments


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["context_id=x", 7, ["a"], True])
async def test_legacy_non_object_arguments_is_invalid_params(app, arguments):
    send = await app.call(
        _rpc("tools/call", request_id=2, name="list_contexts", arguments=arguments),
        headers={b"mcp-session-id": SESSION_ID},
    )

    assert send.status == 200
    assert send.body["error"] == {
        "code": -32602,
        "message": "Invalid params: 'arguments' must be an object",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["context_id=x", 7])
async def test_stateless_non_object_arguments_is_invalid_params(app, arguments):
    body, headers = _modern("tools/call", request_id=2, name="list_contexts", arguments=arguments)
    send = await app.call(body, headers=headers)

    assert send.body["error"]["code"] == -32602
    assert send.body["error"]["message"] == "Invalid params: 'arguments' must be an object"


@pytest.mark.asyncio
async def test_legacy_null_arguments_still_call_the_tool(app):
    send = await app.call(
        _rpc("tools/call", request_id=2, name="list_contexts", arguments=None),
        headers={b"mcp-session-id": SESSION_ID},
    )
    assert send.body["result"]["content"][0]["text"] == '{"status":"success"}'
