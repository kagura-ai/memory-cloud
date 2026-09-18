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

import pytest

from mcp_server.transport import handle_streamable_http_post


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


async def _post(payload: dict) -> _Recorder:
    send = _Recorder()
    await handle_streamable_http_post(
        {"type": "http", "method": "POST", "path": "/mcp"},
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

    # HTTP 200 + JSON-RPC error, like the tools/call error path: this server
    # speaks the legacy (initialize-handshake) protocol, and a 404 + -32601 is
    # the *modern* contract that a dual-era client would read as "modern
    # server, do not fall back to initialize".
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

    # Only the legacy (initialize-handshake) versions this server actually
    # speaks — never 2026-07-28, which would commit us to per-request _meta
    # / stateless semantics we do not implement.
    versions = result["supportedVersions"]
    assert versions and all(v < "2026-07-28" for v in versions)
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
        ("2026-07-28", "2024-11-05"),
        (None, "2024-11-05"),
    ],
)
async def test_initialize_echoes_every_version_discover_advertises(requested, negotiated):
    """``server/discover`` and ``initialize`` must not contradict each other: a
    client that picks a version out of ``supportedVersions`` gets it echoed back
    (spec: a server that supports the requested version MUST answer with it)."""
    params = {} if requested is None else {"protocolVersion": requested}
    send = await _post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    assert send.body["result"]["protocolVersion"] == negotiated


@pytest.mark.asyncio
async def test_every_advertised_version_is_negotiable():
    disc = await _post({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
    for version in disc.body["result"]["supportedVersions"]:
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
