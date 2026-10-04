"""Compact write acknowledgements (#1851).

``remember`` / ``update_memory`` used to repeat the same ~400-character
``persistence.detail`` prose on every write. The block now carries only its
machine-readable fields; ``verbose=true`` adds the prose back, and
``guide(["persistence"])`` holds it on demand.
"""

from __future__ import annotations

import json

import pytest

from mcp_server.tools._definitions import get_tool_definitions
from mcp_server.tools._helpers import _persistence_response_field
from mcp_server.tools.memory import handle_remember, handle_update_memory
from models.schemas import PersistenceInfo

COMPACT_KEYS = {"scope", "committed", "promotes_via", "consolidation_archive_min_age_days"}


def _info() -> PersistenceInfo:
    return PersistenceInfo(
        scope="working",
        committed=True,
        promotes_via="consolidation",
        consolidation_archive_min_age_days=7,
        detail="Committed and durable now — 'working' is a lifecycle label.",
    )


def test_default_persistence_block_has_no_detail_prose():
    block = _persistence_response_field(_info())["persistence"]
    assert set(block) == COMPACT_KEYS
    assert block == {
        "scope": "working",
        "committed": True,
        "promotes_via": "consolidation",
        "consolidation_archive_min_age_days": 7,
    }


def test_verbose_restores_the_full_block():
    block = _persistence_response_field(_info(), verbose=True)["persistence"]
    assert set(block) == COMPACT_KEYS | {"detail"}
    assert block["detail"].startswith("Committed and durable now")


def test_missing_persistence_is_omitted_either_way():
    assert _persistence_response_field(None) == {}
    assert _persistence_response_field(None, verbose=True) == {}


@pytest.mark.parametrize("name", ["remember", "update_memory"])
def test_write_tools_offer_verbose_and_point_to_the_guide(name):
    tool = next(t for t in get_tool_definitions() if t["name"] == name)
    verbose = tool["inputSchema"]["properties"]["verbose"]
    assert verbose["type"] == "boolean"
    assert "persistence" in verbose["description"]
    assert "verbose" not in tool["inputSchema"]["required"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "args"),
    [
        (
            handle_remember,
            {
                "context_id": "00000000-0000-4000-8000-000000000001",
                "summary": "s",
                "content": "c",
                "type": "note",
                "verbose": "yes",
            },
        ),
        (
            handle_update_memory,
            {
                "context_id": "00000000-0000-4000-8000-000000000001",
                "memory_id": "00000000-0000-4000-8000-000000000002",
                "verbose": 1,
            },
        ),
    ],
)
async def test_verbose_must_be_a_boolean(handler, args):
    """Rejected before any database access: a string 'false' must not read as true."""
    (block,) = await handler(args, "user-1", None)
    payload = json.loads(block.text)
    assert payload["error"] == "validation_error"
    assert "verbose" in payload["message"]
