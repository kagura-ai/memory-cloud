"""#1750: get_context_info ``stats.details.by_type`` keeps the 20 largest types.

``type`` is free text (up to 50 characters), so a context can hold any number
of distinct types and ``by_type`` had no cap. The MCP reply keeps the 20 types
with the most memories and folds the rest into ``other``; ``by_type_truncated``
and ``by_type_total_types`` say that it did.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.context import _cap_by_type, handle_get_context_info


def _get_db_yielding(db):
    async def mock_get_db():
        yield db

    return mock_get_db


async def _details(by_type: dict[str, int]) -> dict:
    context = SimpleNamespace(
        id=uuid4(),
        name="dev",
        display_name="dev",
        summary=None,
        usage_guide=None,
        is_private=True,
        is_locked=False,
        workspace_id=None,
    )
    db = AsyncMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=exec_result)
    stats = SimpleNamespace(
        total_count=sum(by_type.values()),
        working_count=0,
        persistent_count=sum(by_type.values()),
        by_type=by_type,
        by_importance={},
        recent_activity=0,
    )
    service = MagicMock()
    service.get_stats = AsyncMock(return_value=stats)
    with (
        patch("db.base.get_db", new=_get_db_yielding(db)),
        patch(
            "mcp_server.tools.context._resolve_context_for_read",
            new=AsyncMock(return_value=context),
        ),
        patch("mcp_server.tools.context._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        result = await handle_get_context_info(
            {"context_id": str(uuid4())}, user_id="u1", workspace_id=None
        )
    return json.loads(result[0].text)["stats"]["details"]


@pytest.mark.asyncio
async def test_many_types_keep_the_top_20_and_fold_the_rest_into_other():
    by_type = {f"type-{i:03d}": 1000 - i for i in range(300)}
    details = await _details(by_type)
    kept = details["by_type"]
    assert len(kept) == 21  # 20 types + other
    assert all(kept[f"type-{i:03d}"] == 1000 - i for i in range(20))
    assert kept["other"] == sum(1000 - i for i in range(20, 300))
    assert sum(kept.values()) == sum(by_type.values())
    assert details["by_type_truncated"] is True
    assert details["by_type_total_types"] == 300


@pytest.mark.asyncio
async def test_20_types_or_fewer_are_returned_unchanged_without_flags():
    by_type = {f"t{i}": i + 1 for i in range(20)}
    details = await _details(by_type)
    assert details["by_type"] == by_type
    assert "by_type_truncated" not in details
    assert "by_type_total_types" not in details


def test_a_stored_other_type_absorbs_the_folded_count():
    by_type = {f"t{i:02d}": 100 - i for i in range(25)}
    by_type["other"] = 99  # a real type named "other", within the top 20
    kept, truncated, total = _cap_by_type(by_type)
    assert truncated is True
    assert total == 26
    assert len(kept) == 20  # "other" is one of the 20, so no 21st key
    assert sum(kept.values()) == sum(by_type.values())


def test_ties_break_by_name_so_the_result_is_stable():
    by_type = {f"t{i:02d}": 1 for i in range(30)}
    kept, _, _ = _cap_by_type(by_type)
    assert [k for k in kept if k != "other"] == [f"t{i:02d}" for i in range(20)]
    assert kept["other"] == 10
