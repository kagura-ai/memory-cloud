"""Argument validation of the ``list`` and ``changes_since`` handlers (#1852).

The SQL is covered on real Postgres in tests/services/test_memory_listing.py;
these cases fail before any database access and pin the caller-error envelopes
and the dispatcher's manual pointer.
"""

from __future__ import annotations

import json

import pytest

from mcp_server.tools import _RATE_LIMIT_EXEMPT_TOOLS, _TOOLS_WITHOUT_CONTEXT_ID, execute_tool_call
from mcp_server.tools._annotations import TOOL_ANNOTATIONS
from mcp_server.tools._profiles import CORE_TOOLS
from mcp_server.tools.guide import GUIDE_INDEX
from mcp_server.tools.listing import handle_changes_since, handle_list

CTX = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("name", ["list", "changes_since"])
def test_the_lanes_are_core_read_tools_that_need_a_context(name):
    assert name in CORE_TOOLS
    assert TOOL_ANNOTATIONS[name]["readOnlyHint"] is True
    assert name not in _TOOLS_WITHOUT_CONTEXT_ID  # a context_id is required
    assert name in _RATE_LIMIT_EXEMPT_TOOLS  # plain SQL, like load_pinned
    assert name in GUIDE_INDEX


async def _err(handler, args):
    (block,) = await handler(args, "user-1", None)
    return json.loads(block.text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "error", "needle"),
    [
        ({"context_id": CTX, "order_by": "summary"}, "validation_error", "order_by"),
        ({"context_id": CTX, "order_by": ["updated_at"]}, "validation_error", "order_by"),
        ({"context_id": CTX, "include_details": "yes"}, "validation_error", "include_details"),
        ({"context_id": CTX, "direction": "up"}, "validation_error", "direction"),
        ({"context_id": CTX, "cursor": "abc"}, "validation_error", "cursor"),
        ({}, "missing_fields", "context_id"),
    ],
)
async def test_list_rejects_bad_arguments_before_touching_the_database(args, error, needle):
    payload = await _err(handle_list, args)
    assert payload["status"] == "error" and payload["error"] == error
    assert needle in payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "error", "needle"),
    [
        ({"context_id": CTX}, "missing_fields", "since"),
        ({"context_id": CTX, "since": "yesterday"}, "validation_error", "since"),
        (
            {"context_id": CTX, "since": "2026-10-02T00:00:00Z", "until": "2026-10-01T00:00:00Z"},
            "validation_error",
            "until",
        ),
        (
            {"context_id": CTX, "since": "2026-10-02T00:00:00Z", "until": 5},
            "validation_error",
            "until",
        ),
        (
            {"context_id": CTX, "since": "2026-10-01T00:00:00Z", "kinds": ["made"]},
            "validation_error",
            "kinds",
        ),
        (
            {"context_id": CTX, "since": "2026-10-01T00:00:00Z", "kinds": []},
            "validation_error",
            "kinds",
        ),
        (
            {"context_id": CTX, "since": "2026-10-01T00:00:00Z", "cursor": 3},
            "validation_error",
            "cursor",
        ),
    ],
)
async def test_changes_since_rejects_bad_arguments_before_touching_the_database(
    args, error, needle
):
    payload = await _err(handle_changes_since, args)
    assert payload["status"] == "error" and payload["error"] == error
    assert needle in payload["message"]


# ------------------------------------------------- #2044: the session lane

_SINCE = "2026-10-01T00:00:00Z"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "needle"),
    [
        # A session id has the correlation-token shape; anything else is refused.
        ({"session_id": "has space"}, "session_id"),
        ({"session_id": "x" * 129}, "session_id"),
        ({"session_id": 7}, "session_id"),
        # superseded is an edge, not a session's write.
        ({"session_id": "s1", "kinds": ["superseded"]}, "superseded"),
        # restored is a rollback event, listed only with a session.
        ({"kinds": ["created", "restored"]}, "restored"),
        ({"session_id": "s1", "kinds": ["made"]}, "kinds"),
        ({"session_id": "s1", "kinds": [{"k": 1}]}, "kinds"),
    ],
)
async def test_changes_since_session_arguments_are_checked_first(args, needle):
    payload = await _err(handle_changes_since, {"context_id": CTX, "since": _SINCE, **args})
    assert payload["status"] == "error" and payload["error"] == "validation_error"
    assert needle in payload["message"]


@pytest.mark.parametrize(
    ("kinds", "session", "expected"),
    [
        (None, False, ("created", "updated", "superseded", "forgotten")),
        (None, True, ("created", "updated", "forgotten", "restored")),
        (["created", "superseded"], True, ("created",)),  # superseded dropped
        (["restored", "restored"], True, ("restored",)),
        (["forgotten", "created"], False, ("forgotten", "created")),
    ],
)
def test_parse_kinds_per_lane(kinds, session, expected):
    from mcp_server.tools.listing import _parse_kinds

    assert _parse_kinds(kinds, session=session) == expected


def test_changes_since_schema_offers_the_session_filter():
    from mcp_server.tools._definitions import get_tool_definitions

    (tool,) = [t for t in get_tool_definitions() if t["name"] == "changes_since"]
    props = tool["inputSchema"]["properties"]
    assert props["session_id"]["type"] == "string"
    assert "restored" in props["kinds"]["items"]["enum"]


# ------------------------------------------------- #2121: by_me


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"by_me": "true"}, "by_me"),
        ({"by_me": 1}, "by_me"),
        # superseded is an edge, not a recorded write.
        ({"by_me": True, "kinds": ["superseded"]}, "superseded"),
    ],
)
async def test_changes_since_by_me_arguments_are_checked_first(args, needle):
    payload = await _err(handle_changes_since, {"context_id": CTX, "since": _SINCE, **args})
    assert payload["status"] == "error" and payload["error"] == "validation_error"
    assert needle in payload["message"]


def test_changes_since_schema_offers_by_me():
    from mcp_server.tools._definitions import get_tool_definitions

    (tool,) = [t for t in get_tool_definitions() if t["name"] == "changes_since"]
    assert tool["inputSchema"]["properties"]["by_me"]["type"] == "boolean"
    assert "by_me" in tool["description"]


async def _by_me_call(
    args: dict, role: object, linked: frozenset[str] = frozenset()
) -> tuple[dict, object]:
    import uuid
    from collections.abc import AsyncIterator
    from unittest.mock import AsyncMock, MagicMock, patch

    from mcp_server.tools import listing
    from services.memory_listing import ChangePage

    db = MagicMock()

    async def get_db() -> AsyncIterator[object]:
        yield db

    lane = AsyncMock(return_value=ChangePage(changes=[], next_cursor=None))
    context = MagicMock(id=uuid.UUID(CTX), is_private=False)
    with (
        patch("db.base.get_db", get_db),
        patch.object(listing, "_resolve_context_for_read", AsyncMock(return_value=context)),
        patch.object(listing, "_context_response_fields", lambda _c: {}),
        patch.object(listing, "_log_tool_usage", AsyncMock()),
        patch.object(listing, "changes_in_session", lane),
        patch.object(
            listing,
            "changes_since",
            AsyncMock(return_value=ChangePage(changes=[], next_cursor=None)),
        ),
        patch("services.session_rollback.context_role", AsyncMock(return_value=role)),
        patch(
            "services.identity_link_service.linked_user_ids",
            AsyncMock(side_effect=lambda _db, uid: frozenset({uid, *linked})),
        ),
    ):
        (block,) = await listing.handle_changes_since(
            {"context_id": CTX, "since": _SINCE, **args}, "caller", None
        )
    return json.loads(block.text), lane


@pytest.mark.asyncio
async def test_by_me_is_the_callers_own_principal_even_for_an_owner():
    from auth.workspace_roles import ContextRole
    from services.write_provenance import SessionPrincipal

    payload, lane = await _by_me_call({"by_me": True}, ContextRole.OWNER)
    kwargs = lane.await_args.kwargs
    assert kwargs["principal"] == SessionPrincipal("caller", None)
    assert kwargs["session_id"] is None
    assert "restored" in kwargs["kinds"] and "superseded" not in kwargs["kinds"]
    assert payload["by_me"] is True and "session_id" not in payload

    # With session_id, by_me narrows an owner's view of the session to them.
    payload, lane = await _by_me_call({"by_me": True, "session_id": "s1"}, ContextRole.OWNER)
    assert lane.await_args.kwargs["principal"] == SessionPrincipal("caller", None)
    assert lane.await_args.kwargs["session_id"] == "s1"
    # Without by_me the owner override of #2044 is unchanged.
    _, lane = await _by_me_call({"session_id": "s1"}, ContextRole.OWNER)
    assert lane.await_args.kwargs["principal"] is None


@pytest.mark.asyncio
async def test_by_me_null_is_omitted():
    from auth.workspace_roles import ContextRole

    payload, lane = await _by_me_call({"by_me": None}, ContextRole.EDITOR)
    assert payload["status"] == "success" and "by_me" not in payload
    lane.assert_not_awaited()  # the plain change log, not the revision lane


@pytest.mark.asyncio
async def test_by_me_allows_restored():
    from auth.workspace_roles import ContextRole

    payload, lane = await _by_me_call({"by_me": True, "kinds": ["restored"]}, ContextRole.EDITOR)
    assert payload["status"] == "success"
    assert lane.await_args.kwargs["kinds"] == ("restored",)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["list", "changes_since"])
async def test_the_dispatcher_points_a_missing_context_at_the_manual(name):
    (block,) = await execute_tool_call(name, {}, "user-1", None)
    payload = json.loads(block.text)
    assert payload["error"] == "context_id_required"
    assert f'guide(["{name}"])' in payload["help"]


# ----------------------------------------------- truncation keeps the cursor honest


def test_bound_points_the_next_cursor_at_the_first_cut_item():
    from mcp_server.tools.listing import _bound

    items = [{"memory_id": str(i), "summary": "x" * 200} for i in range(5)]
    envelope, kept = _bound({"status": "success", "memories": items}, "memories", 600)
    assert 0 < kept < 5
    assert envelope["truncated"] is True and len(envelope["memories"]) == kept
    # even an oversized single item is kept rather than returning an empty page
    big = [{"memory_id": "0", "summary": "y" * 5000}]
    envelope, kept = _bound({"status": "success", "memories": big}, "memories", 100)
    assert kept == 1 and envelope["memories"] == big


def test_bound_reserves_room_for_the_flags_and_the_cursor():
    """The flags and continuation cursor are added after the cut; the final
    reply must still fit max_chars (#1743 contract)."""
    import json

    from mcp_server.tools.listing import _bound

    items = [
        {"memory_id": f"id-{i}", "summary": "s" * 40, "details": {"k": "v" * 40}} for i in range(20)
    ]
    for max_chars in range(300, 1200, 37):
        envelope, kept = _bound(
            {"status": "success", "memories": items, "next_cursor": None}, "memories", max_chars
        )
        if kept < len(items):
            envelope["next_cursor"] = "c" * 90  # a changes_since-sized token
        assert len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))) <= max_chars, (
            max_chars
        )


@pytest.mark.asyncio
async def test_session_lane_counts_a_linked_identity_as_the_caller():
    """#2114: by_me and a named session cover the caller's link set."""
    from auth.workspace_roles import ContextRole
    from services.write_provenance import SessionPrincipal

    expected = SessionPrincipal("caller", None, frozenset({"caller", "other"}))
    _, lane = await _by_me_call({"by_me": True}, ContextRole.OWNER, frozenset({"other"}))
    assert lane.await_args.kwargs["principal"] == expected
    _, lane = await _by_me_call({"session_id": "s1"}, ContextRole.EDITOR, frozenset({"other"}))
    assert lane.await_args.kwargs["principal"] == expected
    # The owner override (user credential) is unchanged.
    _, lane = await _by_me_call({"session_id": "s1"}, ContextRole.OWNER, frozenset({"other"}))
    assert lane.await_args.kwargs["principal"] is None
