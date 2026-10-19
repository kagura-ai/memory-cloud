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
