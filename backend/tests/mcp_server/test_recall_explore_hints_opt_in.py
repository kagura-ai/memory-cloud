"""``include_explore_hints`` on a URL that does not list ``explore`` (#2470).

A hint is a seed for ``explore()``. Where the URL does not list that tool the
service is not asked for hints, and the reply says why instead of leaving the
key out as though there were none. See ``docs/mcp-tools.md``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools._helpers import set_mcp_tool_view_query
from mcp_server.tools.memory import handle_recall
from models.schemas import ExploreHint, RecallResponse


@pytest.fixture(autouse=True)
def _reset_view():
    set_mcp_tool_view_query(None)
    yield
    set_mcp_tool_view_query(None)


async def _recall(args: dict[str, Any]) -> tuple[dict, Any]:
    """``handle_recall`` over stubs; returns the reply and the service's request."""
    db = AsyncMock()

    async def fake_get_db():
        yield db

    seen: dict[str, Any] = {}

    async def fake_recall(request, **_kwargs):
        seen["request"] = request
        hints = [ExploreHint(memory_id=uuid4(), reason="top_result")]
        return RecallResponse(
            results=[], explore_hints=hints if request.include_explore_hints else None
        )

    service = MagicMock()
    service.recall = fake_recall
    config_repo = MagicMock()
    config_repo.create_or_get = AsyncMock(
        return_value=MagicMock(embedding_model="text-embedding-3-small")
    )
    context = MagicMock(trust_tier="trusted")

    with (
        patch("db.base.get_db", new=fake_get_db),
        patch(
            "mcp_server.tools.memory._resolve_context_for_read",
            new=AsyncMock(return_value=context),
        ),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._touch_context_last_used", new=AsyncMock()),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
        patch(
            "repositories.config_repository.ContextSearchConfigRepository",
            new=MagicMock(return_value=config_repo),
        ),
    ):
        result = await handle_recall(
            {"query": "q", "context_id": str(uuid4()), **args}, user_id="u1", workspace_id=None
        )
    return json.loads(result[0].text), seen["request"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [b"", b"profile=core", b"tools=recall"])
async def test_requested_where_explore_is_not_listed_says_how_to_list_it(query):
    set_mcp_tool_view_query(query)

    reply, request = await _recall({"include_explore_hints": True})

    assert reply["status"] == "success"
    assert request.include_explore_hints is False
    assert "explore_hints" not in reply
    assert "add_tools=explore" in reply["explore_hints_omitted"]
    assert "profile=full" in reply["explore_hints_omitted"]


@pytest.mark.asyncio
async def test_a_broken_url_reports_its_own_error():
    set_mcp_tool_view_query(b"profile=typo")

    reply, request = await _recall({"include_explore_hints": True})

    assert request.include_explore_hints is False
    assert "unknown tool profile 'typo'" in reply["explore_hints_omitted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [b"profile=full", b"add_tools=explore", b"tools=recall,explore"])
async def test_requested_where_explore_is_listed_returns_hints(query):
    set_mcp_tool_view_query(query)

    reply, request = await _recall({"include_explore_hints": True})

    assert request.include_explore_hints is True
    assert [hint["reason"] for hint in reply["explore_hints"]] == ["top_result"]
    assert "explore_hints_omitted" not in reply


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{}, {"include_explore_hints": False}])
async def test_not_requested_adds_nothing(args):
    set_mcp_tool_view_query(b"")

    reply, _request = await _recall(args)

    assert "explore_hints" not in reply
    assert "explore_hints_omitted" not in reply
