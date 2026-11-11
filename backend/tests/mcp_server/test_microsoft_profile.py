"""``?profile=microsoft`` is an alias of the ``google`` view (#2778).

Microsoft 365 Copilot runs only ``readOnlyHint: true`` tools without asking,
asks before every other tool, and its Researcher agent uses read-only tools
only. The ``google`` profile (#2773) already serves what that needs: every
tool with a ``title`` and explicit hints, and ``recall`` read-only with no
learning writes. The owner decided (2026-11-11) that ``microsoft`` is the same
view under a second name: the same tools, prose, annotations, instructions and
replies, from one implementation. These tests hold the alias to every place
that reads the google view; ``test_google_profile`` pins the view itself, and
the core and full surfaces stay pinned by their own tests.
"""

from __future__ import annotations

import contextlib
import json
from uuid import uuid4

import pytest

from mcp_server.tools import execute_tool_call
from mcp_server.tools._annotations import TOOL_ANNOTATIONS
from mcp_server.tools._google_profile import GOOGLE_INSTRUCTIONS_BASE
from mcp_server.tools._helpers import _mcp_tool_view_query
from mcp_server.tools._profiles import (
    CORE_TOOLS,
    GOOGLE_TOOLS,
    MICROSOFT_PROFILE,
    PROFILES,
    is_google_view,
    not_listed_reason,
    select_tool_definitions,
)
from tests.mcp_server.test_google_profile import (  # noqa: F401 - fixtures
    _instructions,
    _payload,
    _recall_kwargs,
    digest,
    handlers,
)
from tests.mcp_server.test_transport_tool_profiles import (
    _legacy,
    _modern_headers,
    _modern_request,
    _stateless,
)

MICROSOFT = b"profile=microsoft"

# What Copilot asks the user to confirm on every call: the tools whose
# ``readOnlyHint`` is false (docs/microsoft-365-copilot.md lists them).
CONFIRMED = {
    "remember",
    "remember_batch",
    "update_memory",
    "forget",
    "create_edge",
    "create_context",
    "update_context",
    "delete_context",
    "feedback",
}


@contextlib.contextmanager
def _view(query: bytes):
    token = _mcp_tool_view_query.set(query)
    try:
        yield
    finally:
        _mcp_tool_view_query.reset(token)


# ------------------------------------------------------------------ the tool set


def test_microsoft_is_a_profile_with_the_google_tools():
    assert MICROSOFT_PROFILE == "microsoft"
    assert PROFILES["microsoft"] == PROFILES["google"] == GOOGLE_TOOLS
    # The other profiles are untouched.
    assert PROFILES["core"] == CORE_TOOLS
    assert PROFILES["full"] is None


@pytest.mark.parametrize(
    "suffix",
    [
        b"",
        b"&add_tools=explore",
        b"&add_tools=describe_tools,guide",
        b"&context=" + str(uuid4()).encode(),
    ],
)
def test_the_microsoft_list_is_the_google_list(suffix):
    assert select_tool_definitions(MICROSOFT + suffix) == select_tool_definitions(
        b"profile=google" + suffix
    )


def test_add_tools_cannot_bring_back_the_discovery_and_manual_tools():
    names = {t["name"] for t in select_tool_definitions(MICROSOFT + b"&add_tools=guide,explore")}
    assert "guide" not in names and "describe_tools" not in names
    assert "explore" in names


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (b"profile=microsoft", True),
        (b"profile=%20microsoft%20&context=x", True),
        ("profile=microsoft", True),
        (b"add_tools=explore&profile=microsoft", True),
        (b"profile=Microsoft", False),
        (b"tools=recall&profile=microsoft", False),
        (b"profile=core&profile=microsoft", False),  # the first value wins
        (b"profile=google", True),
        (b"profile=core", False),
        (None, False),
    ],
)
def test_the_microsoft_url_is_the_google_view(query, expected):
    assert is_google_view(query) is expected


# --------------------------------------------------------------- annotations


def test_every_tool_has_a_title_and_an_explicit_read_only_hint():
    """The issue's done-when: what Copilot's validation asks of each tool."""
    tools = select_tool_definitions(MICROSOFT)
    assert tools
    for tool in tools:
        annotations = tool["annotations"]
        assert isinstance(tool["title"], str) and tool["title"].strip(), tool["name"]
        assert annotations["title"] == tool["title"], tool["name"]
        assert isinstance(annotations["readOnlyHint"], bool), tool["name"]
        assert isinstance(annotations["destructiveHint"], bool), tool["name"]


def test_search_is_read_only_and_the_writers_ask_for_confirmation():
    tools = {t["name"]: t["annotations"] for t in select_tool_definitions(MICROSOFT)}
    assert tools["recall"]["readOnlyHint"] is True
    assert tools["recall"]["destructiveHint"] is False
    assert {name for name, a in tools.items() if not a["readOnlyHint"]} == CONFIRMED
    for name in CONFIRMED:  # the writers keep their honest hints
        assert tools[name] == TOOL_ANNOTATIONS[name]
    # The global table (and with it OAuth scope selection) is unchanged.
    assert TOOL_ANNOTATIONS["recall"]["readOnlyHint"] is False


# ---------------------------------------------------------------- transports


@pytest.mark.asyncio
@pytest.mark.parametrize("drive", [_legacy, _stateless], ids=["session", "stateless"])
async def test_both_transports_serve_the_google_list(drive):
    microsoft, google = await drive(MICROSOFT), await drive(b"profile=google")
    assert microsoft.status == google.status == 200
    assert microsoft.body["result"]["tools"] == google.body["result"]["tools"]


# --------------------------------------------------------------- instructions


@pytest.mark.asyncio
async def test_the_instructions_are_the_google_instructions(digest):  # noqa: F811
    assert await _instructions(MICROSOFT) == (GOOGLE_INSTRUCTIONS_BASE, False)
    guarded = b"&guardrails=" + str(uuid4()).encode()
    assert await _instructions(MICROSOFT + guarded) == await _instructions(
        b"profile=google" + guarded
    )
    context = b"&context=" + str(uuid4()).encode()
    assert await _instructions(MICROSOFT + context) == await _instructions(
        b"profile=google" + context
    )
    assert digest.digest_reads == 0


@pytest.mark.asyncio
async def test_the_rest_preview_of_a_microsoft_url_is_the_google_base(monkeypatch):
    from unittest.mock import AsyncMock

    import services.guardrail_digest as digest_mod
    from api.routes.memory import guardrail_digest
    from services.guardrail_digest import DigestEntries, DigestEntry

    ctx = uuid4()
    entries = DigestEntries(
        context_id=ctx,
        entries=[DigestEntry(str(uuid4()), "a rule", 0.8, True, "manual")],
        total_available=1,
        truncated=False,
        tool_triggered_version="0123456789abcdef",
    )
    monkeypatch.setattr(digest_mod, "fetch_entries", AsyncMock(return_value=entries))

    response = await guardrail_digest(
        user={"user_id": "u1"},
        db=AsyncMock(),
        context_id=str(ctx),
        target="instructions",
        profile="microsoft",
        tools=None,
        add_tools=None,
    )

    assert response.body.decode() == GOOGLE_INSTRUCTIONS_BASE


# --------------------------------------------------------------------- replies


@pytest.mark.asyncio
async def test_unknown_tool_help_is_the_google_help(handlers):  # noqa: F811
    replies = []
    for query in (MICROSOFT, b"profile=google"):
        with _view(query):
            replies.append(_payload(await execute_tool_call("no_such_tool", {}, "u", None)))
    assert replies[0] == replies[1]
    assert "describe_tools" not in json.dumps(replies[0])


@pytest.mark.asyncio
async def test_a_caller_error_gets_no_manual_pointer(handlers):  # noqa: F811
    handlers["recall"] = (True, {"status": "error", "error": "validation_error", "message": "m"})
    with _view(MICROSOFT):
        result = await execute_tool_call(
            "recall", {"query": "q", "context_id": str(uuid4())}, "u", None
        )
    assert "help" not in _payload(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_context_info", "bootstrap"])
async def test_the_quick_reference_is_left_out(handlers, tool):  # noqa: F811
    from mcp_server.tools._constants import KAGURA_MEMORY_INSTRUCTIONS

    handlers[tool] = (False, {"status": "success", "instructions": KAGURA_MEMORY_INSTRUCTIONS})
    with _view(MICROSOFT):
        payload = _payload(await execute_tool_call(tool, {"context_id": str(uuid4())}, "u", None))
    assert "instructions" not in payload


def test_a_hidden_tool_reason_is_the_google_reason():
    assert not_listed_reason("explore", MICROSOFT) == not_listed_reason(
        "explore", b"profile=google"
    )


# ---------------------------------------------------------------- recall writes


@pytest.mark.asyncio
async def test_microsoft_recall_asks_the_service_for_no_learning(monkeypatch):
    from mcp_server.tools.memory import handle_recall

    seen = _recall_kwargs(monkeypatch)
    with _view(MICROSOFT), contextlib.suppress(RuntimeError):  # the fake stops the call
        await handle_recall({"query": "q", "context_id": str(uuid4())}, "u", uuid4())
    assert seen["learning"] is False


# ------------------------------------------------------- no redirects (item 3)


_WORKSPACE = "3f2b8c1e-5d6a-4b7c-9e0f-1a2b3c4d5e6f"


def _app_client(monkeypatch):
    """The FastAPI app with MCP auth stubbed, as a connector would reach it.

    The stub answers like a key scoped to ``_WORKSPACE``, so ``/mcp/w/{id}``
    matches without a membership read (the OAuth membership check runs on the
    database in ``tests/integration/test_oauth_cross_workspace_client_db.py``).
    """
    from uuid import UUID

    from fastapi.testclient import TestClient

    import mcp_server.transport as transport
    from api.main import app

    async def fake_auth(**_kwargs):
        return "user-1", None, UUID(_WORKSPACE)

    async def fake_workspace(_user_id):
        return None

    monkeypatch.setattr(transport, "authenticate_mcp_request", fake_auth)
    monkeypatch.setattr(transport, "_get_user_workspace_id", fake_workspace)
    return TestClient(app, base_url="https://testserver")


_PATHS = [
    "/mcp",
    "/mcp/",
    f"/mcp/w/{_WORKSPACE}",
    f"/mcp/w/{_WORKSPACE}/",
]


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("query", ["?profile=microsoft", ""])
def test_the_mcp_url_answers_without_a_redirect(monkeypatch, path, query):
    """Copilot's validation refuses redirects: every form a connector may
    register answers the request itself, with or without a trailing slash."""
    client = _app_client(monkeypatch)
    body = _modern_request()
    headers = {k.decode(): v.decode() for k, v in _modern_headers(body).items()}
    headers["accept"] = "application/json, text/event-stream"

    response = client.post(path + query, json=body, headers=headers, follow_redirects=False)

    assert response.status_code == 200, response.text[:300]
    assert "location" not in response.headers
    listed = [tool["name"] for tool in response.json()["result"]["tools"]]
    assert listed == list(GOOGLE_TOOLS if query else CORE_TOOLS)


@pytest.mark.parametrize("path", _PATHS)
def test_an_unauthenticated_request_is_challenged_in_place(path):
    """Before sign-in a connector gets the 401 challenge, not a redirect."""
    from fastapi.testclient import TestClient

    from api.main import app

    response = TestClient(app, base_url="https://testserver").post(
        path + "?profile=microsoft", json=_modern_request(), follow_redirects=False
    )

    assert response.status_code == 401
    assert "location" not in response.headers
    assert "resource_metadata=" in response.headers["www-authenticate"]
