"""``update_memory``'s add_tags / remove_tags / details_patch over MCP (#2585).

The definition declares them, the dispatcher passes them through (the schema
is closed: an undeclared argument would be refused), a conflict with ``tags``
/ ``details`` or with upsert mode is ``validation_error`` before any database
work, and a status flip runs end to end through the real service to the
success envelope. The merge rules themselves are tested on the service
(``tests/services/test_update_memory.py``).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools.memory import handle_update_memory
from services.memory_service import MemoryService

_CTX = "00000000-0000-4000-8000-000000000001"
_MEM = "00000000-0000-4000-8000-000000000002"


def _properties() -> dict:
    tool = next(t for t in get_tool_definitions() if t["name"] == "update_memory")
    return tool["inputSchema"]["properties"]


def test_the_definition_declares_the_three_parameters():
    props = _properties()
    assert props["add_tags"]["type"] == "array"
    assert props["add_tags"]["items"] == {"type": "string"}
    assert props["remove_tags"]["type"] == "array"
    assert props["details_patch"]["type"] == "object"
    assert "null" in props["details_patch"]["description"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "phrase"),
    [
        ({"tags": ["a"], "add_tags": ["b"]}, "give one or the other"),
        ({"tags": ["a"], "remove_tags": ["b"]}, "give one or the other"),
        ({"details": {"a": 1}, "details_patch": {"b": 2}}, "details_patch merges"),
        ({"add_tags": ["t"] * 51}, "at most 50 tags"),
    ],
    ids=["tags+add_tags", "tags+remove_tags", "details+details_patch", "add_tags-cap"],
)
async def test_a_conflict_is_a_validation_error_from_the_dispatcher(args, phrase):
    """Through ``execute_tool_call``: the closed schema accepts the new names and
    the request model refuses the pair before any database work."""
    from mcp_server.tools import execute_tool_call

    (block,) = await execute_tool_call(
        "update_memory", {"context_id": _CTX, "memory_id": _MEM, **args}, "user-1", None
    )
    payload = json.loads(block.text)
    assert payload["status"] == "error"
    assert payload["error"] == "validation_error"
    assert phrase in payload["message"]


@pytest.mark.asyncio
async def test_upsert_mode_refuses_a_partial_edit():
    (block,) = await handle_update_memory(
        {
            "context_id": _CTX,
            "external_id": "ext-1",
            "summary": "a summary long enough",
            "content": "c",
            "type": "note",
            "add_tags": ["x"],
        },
        user_id="user-1",
        workspace_id=None,
    )
    payload = json.loads(block.text)
    assert payload["error"] == "validation_error"
    assert "use tags / details with external_id" in payload["message"]


def _stored_row():
    memory = MagicMock()
    memory.id = uuid4()
    memory.point_id = memory.id
    memory.user_id = "user-1"
    memory.workspace_id = uuid4()
    memory.context_id = uuid4()
    memory.summary = "stored summary text"
    memory.context_summary = None
    memory.content = "stored content"
    memory.details = {"file": "a.py"}
    memory.type = "tech-debt"
    memory.importance = 0.45
    memory.tags = ["tech-debt", "status:open", "area:mcp"]
    memory.context = None
    memory.scope = "working"
    memory.delivery_mode = "on_recall"
    memory.is_pinned = False
    memory.updated_at = None
    memory.deleted_at = None
    memory.supersede_candidate = None
    memory.version = 2
    return memory


async def _call(memory, args):
    """Run the handler against the real service; only its I/O edges are stubbed.

    The methods are patched on the class rather than the class in its module:
    the service's static helpers look ``MemoryService`` up by name.
    """
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.rollback = AsyncMock()

    async def get_db():
        yield db

    ctx = MagicMock(id=uuid4(), display_name="Ctx", is_private=False, is_locked=False)
    ctx.name = "ctx"
    with (
        patch("db.base.get_db", new=get_db),
        patch("mcp_server.tools.memory._resolve_context", new=AsyncMock(return_value=ctx)),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("mcp_server.tools.memory._touch_context_last_used", new=AsyncMock()),
        patch("services.memory_access_event_writer.emit_memory_access_event", new=AsyncMock()),
        patch.object(MemoryService, "_update_load_authorized", new=AsyncMock(return_value=memory)),
        patch.object(MemoryService, "_lock_for_edit", new=AsyncMock(return_value=memory)),
        patch.object(MemoryService, "_ensure_capacity", new=AsyncMock()),
        patch.object(MemoryService, "_sync_metadata_payload", new=AsyncMock()),
        patch.object(MemoryService, "_lint_write", new=AsyncMock(return_value=[])),
    ):
        (block,) = await handle_update_memory(
            {"context_id": str(ctx.id), **args}, user_id="user-1", workspace_id=None
        )
    return json.loads(block.text), db


@pytest.mark.asyncio
async def test_a_status_flip_is_one_call_that_touches_nothing_else():
    memory = _stored_row()
    payload, _db = await _call(
        memory,
        {
            "memory_id": str(memory.id),
            "remove_tags": ["status:open"],
            "add_tags": ["status:done"],
        },
    )
    assert payload["status"] == "success"
    assert payload["operation"] == "updated"
    assert payload["re_embedded"] is False
    assert payload["version"] == 3
    assert memory.tags == ["tech-debt", "area:mcp", "status:done"]
    assert memory.details == {"file": "a.py"}
    assert memory.summary == "stored summary text"


@pytest.mark.asyncio
async def test_a_details_patch_through_the_tool():
    memory = _stored_row()
    payload, _db = await _call(
        memory, {"memory_id": str(memory.id), "details_patch": {"file": None, "pr": 2600}}
    )
    assert payload["status"] == "success"
    assert memory.details == {"pr": 2600}
    assert memory.tags == ["tech-debt", "status:open", "area:mcp"]


@pytest.mark.asyncio
async def test_a_result_over_the_caps_is_a_validation_error_and_writes_nothing():
    memory = _stored_row()
    memory.tags = [f"t{i}" for i in range(50)]
    payload, db = await _call(memory, {"memory_id": str(memory.id), "add_tags": ["one-more"]})
    assert payload["error"] == "validation_error"
    assert "at most 50 tags (got 51)" in payload["message"]
    assert len(memory.tags) == 50
    db.commit.assert_not_awaited()
    db.rollback.assert_awaited()
