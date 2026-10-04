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
