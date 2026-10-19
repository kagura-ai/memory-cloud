"""``?context=<uuid>`` reaches the tool dispatcher on both transport eras (#2125).

The parse itself is pinned in ``test_default_context``. Here a real
``tools/call`` is driven through ``mcp_asgi_app`` (auth and the session
manager stubbed, as in ``test_transport_tool_profiles``) and the dispatcher
records the selection it sees.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

import mcp_server.tools as tools_mod
from mcp_server.tools._default_context import DefaultContextSelection
from mcp_server.tools._helpers import get_mcp_default_context_selection
from tests.mcp_server.test_transport_tool_profiles import (
    _legacy_request,
    _modern_headers,
    _modern_request,
    _receive_for,
    _Recorder,
    asgi,  # noqa: F401 - pytest fixture
)


@pytest.fixture
def seen(monkeypatch):
    """Replace the dispatcher with one that records the request's selection."""
    recorded: dict = {}

    async def fake_execute(**kwargs):
        recorded["selection"] = get_mcp_default_context_selection()
        recorded["arguments"] = kwargs["arguments"]
        return [SimpleNamespace(type="text", text="ok")]

    monkeypatch.setattr(tools_mod, "execute_tool_call", fake_execute)
    return recorded


def _call(era: str) -> tuple[dict, dict[bytes, bytes]]:
    if era == "stateless":
        body = _modern_request("tools/call", name="get_usage", arguments={})
        return body, _modern_headers(body)
    return _legacy_request("tools/call", name="get_usage", arguments={}), {
        b"mcp-session-id": b"sess-1"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("era", ["session", "stateless"])
async def test_the_url_default_reaches_the_dispatcher(asgi, seen, era):  # noqa: F811
    ctx = uuid4()
    body, headers = _call(era)

    send = await asgi(body, headers, f"profile=core&context={ctx}".encode())

    assert send.status == 200
    assert seen["selection"] == DefaultContextSelection("url", ctx)
    # The transport never edits the arguments; the dispatcher fills them.
    assert seen["arguments"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("era", ["session", "stateless"])
async def test_without_the_parameter_the_selection_is_none(asgi, seen, era):  # noqa: F811
    body, headers = _call(era)

    await asgi(body, headers, b"profile=core")

    assert seen["selection"] == DefaultContextSelection("none")


@pytest.mark.asyncio
@pytest.mark.parametrize("era", ["session", "stateless"])
async def test_an_invalid_value_does_not_fail_the_request(asgi, seen, era):  # noqa: F811
    body, headers = _call(era)

    send = await asgi(body, headers, b"context=my-project")

    assert send.status == 200
    assert seen["selection"].mode == "invalid"
    assert seen["selection"].raw_length == len("my-project")


@pytest.mark.asyncio
async def test_workspace_route_keeps_the_context_parameter(monkeypatch):
    """``/mcp/w/{workspace_id}?context=<uuid>``: the FastAPI route rebuilds the
    scope and must not drop the query string doing so."""
    from starlette.requests import Request

    import api.main as main

    captured: dict = {}

    async def capture(scope, _receive, _send):
        captured.update(scope)

    monkeypatch.setattr(main, "mcp_asgi_app", capture)

    async def receive():  # pragma: no cover - the capture never reads the body
        return {"type": "http.disconnect"}

    query = f"context={uuid4()}".encode()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp/w/ws-1",
            "query_string": query,
            "headers": [],
        },
        receive,
        SimpleNamespace(),
    )
    await main.mcp_workspace_handler(request, workspace_id="ws-1", path="")

    assert captured["query_string"] == query


# ------------------------------------------------------------- instructions


@pytest.fixture
def reader(monkeypatch):
    """A fake DB session and resolver behind the instructions' name lookup."""
    from unittest.mock import AsyncMock

    import db.base as db_base
    from config.settings import get_settings
    from services.permission_service import PermissionService

    state = SimpleNamespace(db_calls=0, calls=[], result=None, raise_with=None)

    async def fake_get_db():
        state.db_calls += 1
        yield AsyncMock()

    async def fake_resolve(self, **kwargs):
        state.calls.append(kwargs)
        if state.raise_with is not None:
            raise state.raise_with
        return state.result

    monkeypatch.setattr(db_base, "get_db", fake_get_db)
    monkeypatch.setattr(PermissionService, "resolve_context_for_workspace_read", fake_resolve)
    # Keep the guardrail digest out of the way: no user-credential fallback.
    monkeypatch.setattr(get_settings(), "mcp_guardrail_digest_last_used_fallback", False)
    return state


async def _instructions(query: bytes, key_workspace_id=None) -> tuple[str, bool]:
    from mcp_server.transport import build_instructions

    return await build_instructions(
        user_id="user-1", query_string=query, key_workspace_id=key_workspace_id, era="stateless"
    )


@pytest.mark.asyncio
async def test_a_readable_default_is_named_through_the_read_resolver(reader):
    from mcp_server.transport import default_context_instructions
    from services.capacity_lock import CapacityPolicy

    ctx, key_ws = uuid4(), uuid4()
    reader.result = SimpleNamespace(name="proj-alpha")

    text, private = await _instructions(f"context={ctx}".encode(), key_workspace_id=key_ws)

    assert (text, private) == (default_context_instructions("proj-alpha"), True)
    assert "Default context: proj-alpha;" in text
    (call,) = reader.calls
    assert call["context_id"] == ctx
    assert call["user_id"] == "user-1"
    assert call["key_workspace_id"] == key_ws
    assert call["required_role"] == "viewer"
    assert call["capacity"] is CapacityPolicy.ALLOWED


@pytest.mark.asyncio
async def test_a_denied_default_serves_the_base_text_privately(reader):
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE
    from utils.exceptions import NotFoundException

    reader.raise_with = NotFoundException("Context", "x")

    assert await _instructions(f"context={uuid4()}".encode()) == (SERVER_INSTRUCTIONS_BASE, True)


@pytest.mark.asyncio
async def test_a_seat_refusal_serves_the_base_text_privately(reader):
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE
    from utils.exceptions import FeatureNotAvailableError

    reader.raise_with = FeatureNotAvailableError("no seat", "members")

    assert await _instructions(f"context={uuid4()}".encode()) == (SERVER_INSTRUCTIONS_BASE, True)


@pytest.mark.asyncio
async def test_a_lookup_failure_serves_the_base_text_privately(reader):
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE

    reader.raise_with = RuntimeError("db down")

    assert await _instructions(f"context={uuid4()}".encode()) == (SERVER_INSTRUCTIONS_BASE, True)


@pytest.mark.asyncio
async def test_an_invalid_value_is_private_base_text_without_a_read(reader):
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE

    assert await _instructions(b"context=my-project") == (SERVER_INSTRUCTIONS_BASE, True)
    assert reader.db_calls == 0


@pytest.mark.asyncio
async def test_without_the_parameter_nothing_is_read_and_the_result_is_public(reader):
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE

    assert await _instructions(b"profile=core") == (SERVER_INSTRUCTIONS_BASE, False)
    assert reader.db_calls == 0


@pytest.mark.asyncio
async def test_guardrails_off_keeps_the_default_context_text(reader):
    """The two switches are independent: ``?guardrails=off`` drops the digest,
    not the default context."""
    reader.result = SimpleNamespace(name="proj-alpha")

    text, private = await _instructions(f"guardrails=off&context={uuid4()}".encode())

    assert "Default context: proj-alpha;" in text
    assert private is True


@pytest.mark.asyncio
async def test_discover_with_the_parameter_is_private_and_names_the_default(reader):
    from mcp_server.transport import TOOLS_LIST_TTL_MS
    from mcp_server.transport_stateless import handle_stateless_post

    reader.result = SimpleNamespace(name="proj-alpha")
    body = _modern_request("server/discover")
    send = _Recorder()
    await handle_stateless_post(
        send,
        body,
        _modern_headers(body),
        user_id="user-1",
        workspace_id=None,
        query_string=f"context={uuid4()}".encode(),
    )

    result = send.body["result"]
    assert "Default context: proj-alpha;" in result["instructions"]
    assert result["cacheScope"] == "private"
    assert result["ttlMs"] == TOOLS_LIST_TTL_MS


@pytest.mark.asyncio
async def test_discover_with_a_denied_default_is_still_private(reader):
    from mcp_server.transport import SERVER_INSTRUCTIONS_BASE
    from mcp_server.transport_stateless import handle_stateless_post
    from utils.exceptions import NotFoundException

    reader.raise_with = NotFoundException("Context", "x")
    body = _modern_request("server/discover")
    send = _Recorder()
    await handle_stateless_post(
        send,
        body,
        _modern_headers(body),
        user_id="user-1",
        workspace_id=None,
        query_string=f"context={uuid4()}".encode(),
    )

    result = send.body["result"]
    assert result["instructions"] == SERVER_INSTRUCTIONS_BASE
    assert result["cacheScope"] == "private"


@pytest.mark.asyncio
async def test_legacy_initialize_carries_the_default_context_text(reader):
    from mcp_server.transport import handle_streamable_http_post

    reader.result = SimpleNamespace(name="proj-alpha")
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}},
    }
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/",
        "query_string": f"context={uuid4()}".encode(),
    }
    session = SimpleNamespace(
        session_id="sess-1", user_id="user-1", workspace_id=None, protocol_version=None
    )
    send = _Recorder()
    await handle_streamable_http_post(scope, _receive_for(body), send, session, {})

    assert "Default context: proj-alpha;" in send.body["result"]["instructions"]
