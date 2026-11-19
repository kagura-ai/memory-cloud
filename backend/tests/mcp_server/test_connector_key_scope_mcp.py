"""A connector key on the MCP endpoint (#2929).

A connector key calls the MCP tools in ``CONNECTOR_KEY_MCP_TOOLS`` and no
other. The tool dispatcher checks every ``tools/call`` first, before argument
validation, the rate limit, the default context, the slot and capacity gates
and the handler, so a refused call charges nothing to the daily call limit and
records no usage; the session stays usable for the listed tools.
``tools/list`` names only the listed tools, and the ``initialize`` /
``server/discover`` instructions are the static base text of the URL's view.
With ``CONNECTOR_KEY_SCOPE=log`` every tool is listed and dispatched, the
instructions are built as for any credential, and each call outside the list
logs one ``connector_key_scope_would_refuse`` event. Any other credential is
not affected. The same call through the whole application on real rows (no
usage row, no daily-limit count) is in
``tests/integration/test_connector_key_scope_routes_db.py``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

import mcp_server.tools as tools_mod
from auth.api_keys import VerifiedKey
from auth.connector_key_scope import CONNECTOR_KEY_MCP_TOOLS
from config.settings import get_settings
from mcp_server.auth import (
    ConnectorScopedCredential,
    _verify_api_key,
    authenticate_mcp_request,
    get_mcp_connector_scope,
    set_mcp_connector_scope,
)
from mcp_server.tools import _build_registry, execute_tool_call, get_tool_definitions
from mcp_server.transport import (
    SERVER_INSTRUCTIONS_BASE,
    build_instructions,
    handle_streamable_http_post,
)
from mcp_server.transport_stateless import handle_stateless_post

CONNECTOR_ID = uuid4()
WS = uuid4()
CTX = uuid4()
MODERN = "2026-07-28"
PV_KEY = "io.modelcontextprotocol/protocolVersion"
CAPS_KEY = "io.modelcontextprotocol/clientCapabilities"


@contextmanager
def as_connector(prefix: str = "kagura_conn", connector_id: UUID | None = CONNECTOR_ID):
    """The request authenticated with a connector key (set by the MCP auth seam)."""
    set_mcp_connector_scope(ConnectorScopedCredential(key_prefix=prefix, connector_id=connector_id))
    try:
        yield
    finally:
        set_mcp_connector_scope(None)


@pytest.fixture
def scope_mode(monkeypatch):
    def set_mode(mode: str) -> None:
        monkeypatch.setattr(get_settings(), "connector_key_scope", mode)

    set_mode("enforce")
    return set_mode


def _schema_args(tool: str) -> dict[str, Any]:
    props = next(
        d["inputSchema"].get("properties", {}) for d in get_tool_definitions() if d["name"] == tool
    )
    return {"context_id": str(CTX)} if "context_id" in props else {}


@pytest.fixture
def dispatch(monkeypatch):
    """The real dispatcher over stub handlers; every per-call step observed."""
    handled: list[str] = []

    def stub(name: str):
        async def handler(_args, _user_id, _workspace_id):
            handled.append(name)
            return [tools_mod.TextContent(type="text", text='{"status":"success"}')]

        return handler

    from mcp_server.tools import _capacity_gate, _slot_gate

    rate = AsyncMock(return_value=(True, 1, 1000))
    default = AsyncMock(side_effect=lambda _name, args: (args, None, None))
    slot = AsyncMock(return_value=None)
    capacity = AsyncMock(return_value=None)
    monkeypatch.setattr(tools_mod, "_TOOL_REGISTRY", {n: stub(n) for n in _build_registry()})
    monkeypatch.setattr(tools_mod, "_check_rate_limit", rate)
    monkeypatch.setattr(tools_mod, "_apply_default_context", default)
    monkeypatch.setattr(_slot_gate, "slot_suspension_refusal", slot)
    monkeypatch.setattr(_capacity_gate, "capacity_lock_refusal", capacity)
    return SimpleNamespace(
        handled=handled, rate=rate, default=default, slot=slot, capacity=capacity
    )


def _envelope(result) -> dict:
    return json.loads(result[0].text)


UNLISTED = sorted(set(_build_registry()) - CONNECTOR_KEY_MCP_TOOLS)
LISTED = sorted(CONNECTOR_KEY_MCP_TOOLS)


def test_every_listed_tool_is_in_the_registry() -> None:
    """A renamed tool cannot silently leave the list."""
    assert CONNECTOR_KEY_MCP_TOOLS <= set(_build_registry())
    assert len(UNLISTED) > 40


# ---------------------------------------------------------------------------
# The dispatcher, over the whole registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_unlisted_tool_refuses_a_connector_key_before_anything_runs(
    dispatch, scope_mode
) -> None:
    with patch("auth.connector_key_scope.logger") as logger:
        for tool in UNLISTED:
            with as_connector():
                result = await execute_tool_call(tool, _schema_args(tool), "u1", WS)
            envelope = _envelope(result)
            assert getattr(result, "is_error", False) is True, tool
            assert envelope["status"] == "error", tool
            assert envelope["error"] == "permission_denied", tool
            assert envelope["error_code"] == "AUTH-204", tool
            assert envelope["message"] == "This credential is not accepted for this tool", tool
    assert dispatch.handled == []
    dispatch.rate.assert_not_awaited()
    dispatch.default.assert_not_awaited()
    dispatch.slot.assert_not_awaited()
    dispatch.capacity.assert_not_awaited()
    events = [(c.args[0], c.kwargs["target"]) for c in logger.warning.call_args_list]
    assert events == [("connector_key_scope_refused", tool) for tool in UNLISTED]
    assert all(c.kwargs["surface"] == "mcp" for c in logger.warning.call_args_list)
    assert all(c.kwargs["connector_id"] == str(CONNECTOR_ID) for c in logger.warning.call_args_list)


@pytest.mark.asyncio
async def test_every_listed_tool_is_dispatched_for_a_connector_key(dispatch, scope_mode) -> None:
    with patch("auth.connector_key_scope.logger") as logger:
        for tool in LISTED:
            with as_connector():
                result = await execute_tool_call(tool, _schema_args(tool), "u1", WS)
            assert _envelope(result) == {"status": "success"}, tool
    assert dispatch.handled == LISTED
    logger.warning.assert_not_called()
    # The rate limit counts the listed tools.
    assert dispatch.rate.await_count == len(
        CONNECTOR_KEY_MCP_TOOLS - tools_mod._RATE_LIMIT_EXEMPT_TOOLS
    )


@pytest.mark.asyncio
async def test_the_check_runs_before_argument_validation(dispatch, scope_mode) -> None:
    """An unlisted call with an argument its schema does not declare is the
    connector key refusal, not ``invalid_argument`` naming the tool's arguments."""
    with as_connector():
        result = await execute_tool_call("setup_connector", {"no_such_argument": 1}, "u1", WS)
    envelope = _envelope(result)
    assert (envelope["error"], envelope["error_code"]) == ("permission_denied", "AUTH-204")
    assert "allowed_arguments" not in envelope
    assert dispatch.handled == []


@pytest.mark.asyncio
async def test_an_unknown_tool_name_is_refused_like_any_unlisted_one(dispatch, scope_mode) -> None:
    with as_connector():
        result = await execute_tool_call("no_such_tool", {}, "u1", WS)
    assert _envelope(result)["error_code"] == "AUTH-204"


@pytest.mark.asyncio
async def test_log_mode_dispatches_every_tool_and_logs_each_unlisted_call(
    dispatch, scope_mode
) -> None:
    scope_mode("log")
    with patch("auth.connector_key_scope.logger") as logger:
        for tool in sorted(_build_registry()):
            with as_connector():
                await execute_tool_call(tool, _schema_args(tool), "u1", WS)
    assert dispatch.handled == sorted(_build_registry())
    events = [(c.args[0], c.kwargs["target"]) for c in logger.warning.call_args_list]
    assert events == [("connector_key_scope_would_refuse", tool) for tool in UNLISTED]
    assert "kagura_conn" in repr(logger.warning.call_args_list)  # the prefix, never more


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["enforce", "log"])
async def test_another_credential_sees_no_change(dispatch, scope_mode, mode) -> None:
    scope_mode(mode)
    with patch("auth.connector_key_scope.logger") as logger:
        for tool in sorted(_build_registry()):
            await execute_tool_call(tool, _schema_args(tool), "u1", WS)
    assert dispatch.handled == sorted(_build_registry())
    logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# The transports: tools/list and tools/call
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int:
        return self.messages[0]["status"]

    @property
    def body(self) -> dict:
        return json.loads(b"".join(m.get("body", b"") for m in self.messages[1:]))


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


async def _legacy(query: bytes, method: str, **params) -> _Recorder:
    body = {"jsonrpc": "2.0", "id": 4, "method": method, "params": params}
    scope = {"type": "http", "method": "POST", "path": "/mcp/", "query_string": query}
    send = _Recorder()
    session = SimpleNamespace(session_id="sess-1", user_id="u1", workspace_id=WS)
    await handle_streamable_http_post(scope, _receive_for(body), send, session, {})
    return send


async def _stateless(query: bytes, method: str, **params) -> _Recorder:
    params["_meta"] = {PV_KEY: MODERN, CAPS_KEY: {}}
    body = {"jsonrpc": "2.0", "id": 4, "method": method, "params": params}
    headers = {b"mcp-protocol-version": MODERN.encode(), b"mcp-method": method.encode()}
    if isinstance(params.get("name"), str):
        headers[b"mcp-name"] = params["name"].encode()
    send = _Recorder()
    await handle_stateless_post(
        send, body, headers, user_id="u1", workspace_id=WS, query_string=query
    )
    return send


DRIVERS = pytest.mark.parametrize("drive", [_legacy, _stateless], ids=["session", "stateless"])


def _names(send: _Recorder) -> list[str]:
    return [tool["name"] for tool in send.body["result"]["tools"]]


@DRIVERS
@pytest.mark.asyncio
async def test_tools_list_names_only_the_listed_tools_for_a_connector_key(
    drive, scope_mode
) -> None:
    with as_connector():
        full = await drive(b"profile=full", "tools/list")
        core = await drive(b"", "tools/list")
    registry_order = [t["name"] for t in get_tool_definitions()]
    assert _names(full) == [n for n in registry_order if n in CONNECTOR_KEY_MCP_TOOLS]
    assert set(_names(core)) < CONNECTOR_KEY_MCP_TOOLS
    if drive is _stateless:
        # The list depends on the credential: no shared cache may hold it.
        assert full.body["result"]["cacheScope"] == "private"


@DRIVERS
@pytest.mark.asyncio
async def test_tools_list_is_unchanged_in_log_mode_and_for_other_credentials(
    drive, scope_mode
) -> None:
    plain = await drive(b"profile=full", "tools/list")
    scope_mode("log")
    with as_connector():
        logged = await drive(b"profile=full", "tools/list")
    assert _names(plain) == _names(logged) == [t["name"] for t in get_tool_definitions()]
    if drive is _stateless:
        assert plain.body["result"]["cacheScope"] == logged.body["result"]["cacheScope"] == "public"


@DRIVERS
@pytest.mark.asyncio
async def test_a_refused_tools_call_is_a_tool_result_and_the_session_stays_usable(
    drive, dispatch, scope_mode
) -> None:
    with as_connector():
        refused = await drive(b"", "tools/call", name="create_context", arguments={"name": "x"})
        allowed = await drive(
            b"", "tools/call", name="recall", arguments={"context_id": str(CTX), "query": "q"}
        )
    assert refused.status == 200
    result = refused.body["result"]
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["error_code"] == "AUTH-204"
    assert allowed.status == 200
    assert "isError" not in allowed.body["result"]
    assert dispatch.handled == ["recall"]


# ---------------------------------------------------------------------------
# initialize / server/discover instructions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_connector_key_gets_the_base_instructions_without_any_read(scope_mode) -> None:
    query = f"guardrails={CTX}&context={CTX}".encode()
    with (
        as_connector(),
        patch("mcp_server.transport._read_digest", new=AsyncMock(side_effect=AssertionError)),
        patch(
            "mcp_server.transport._read_default_context_name",
            new=AsyncMock(side_effect=AssertionError),
        ),
    ):
        text, private = await build_instructions(
            user_id="u1", query_string=query, key_workspace_id=WS, era="legacy"
        )
    assert (text, private) == (SERVER_INSTRUCTIONS_BASE, False)


@pytest.mark.asyncio
async def test_a_connector_key_on_the_google_view_gets_its_base_text(scope_mode) -> None:
    from mcp_server.tools._google_profile import GOOGLE_INSTRUCTIONS_BASE

    with as_connector():
        text, private = await build_instructions(
            user_id="u1",
            query_string=f"profile=google&context={CTX}".encode(),
            key_workspace_id=WS,
            era="stateless",
        )
    assert (text, private) == (GOOGLE_INSTRUCTIONS_BASE, False)


@pytest.mark.asyncio
async def test_log_mode_keeps_the_instructions_reads(scope_mode) -> None:
    scope_mode("log")
    query = f"guardrails={CTX}".encode()
    digest = AsyncMock(return_value=None)
    with as_connector(), patch("mcp_server.transport._read_digest", new=digest):
        await build_instructions(
            user_id="u1", query_string=query, key_workspace_id=WS, era="legacy"
        )
    digest.assert_awaited_once()


# ---------------------------------------------------------------------------
# The auth seam
# ---------------------------------------------------------------------------


def _verified(**fields) -> VerifiedKey:
    return VerifiedKey(id=1, user_id="u1", workspace_id=WS, bound_context_id=None, **fields)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verified", "expected"),
    [
        (
            _verified(
                key_prefix="kagura_link",
                is_connector_key=True,
                connector_id=CONNECTOR_ID,
                connector_scoped=True,
            ),
            ConnectorScopedCredential(key_prefix="kagura_link", connector_id=CONNECTOR_ID),
        ),
        (
            _verified(key_prefix="kagura_name", connector_scoped=True),
            ConnectorScopedCredential(key_prefix="kagura_name", connector_id=None),
        ),
        (_verified(key_prefix="kagura_mine"), None),
    ],
    ids=["linked", "reserved-name", "ordinary"],
)
async def test_the_auth_seam_records_a_connector_scoped_key(verified, expected) -> None:
    with patch("auth.dependencies.verify_api_key", new=AsyncMock(return_value=verified)):
        await _verify_api_key("kagura_x")
    assert get_mcp_connector_scope() == expected
    set_mcp_connector_scope(None)


@pytest.mark.asyncio
async def test_every_request_starts_without_a_connector_scope() -> None:
    from mcp_server.auth import MissingCredentialsError

    set_mcp_connector_scope(ConnectorScopedCredential(key_prefix="stale", connector_id=None))
    with pytest.raises(MissingCredentialsError):
        await authenticate_mcp_request(None, None)
    assert get_mcp_connector_scope() is None
