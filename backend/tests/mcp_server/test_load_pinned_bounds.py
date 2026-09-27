"""load_pinned reply budget (#1743).

At the default cap (100) with context summaries of up to 2,000 characters the
reply could pass 200k characters. ``max_chars`` (default 20,000) bounds it:
``context_summary`` goes first (``context_summary_omitted``), then the tail
of the list (``truncated`` with the real ``total_available``).
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.memory import handle_load_pinned
from models.schemas import LoadPinnedResponse, PinnedMemoryItem

NOW = datetime(2026, 9, 27, 9, 0, 0, tzinfo=UTC)


def _items(n: int, *, context_summary: str | None = "c" * 2_000) -> list[PinnedMemoryItem]:
    return [
        PinnedMemoryItem(
            memory_id=uuid4(),
            summary="s" * 400,
            context_summary=context_summary,
            type="decision",
            importance=0.9,
            delivery_mode="always",
            created_at=NOW,
        )
        for _ in range(n)
    ]


def _response(items, *, cap=100, total=None) -> LoadPinnedResponse:
    total = len(items) if total is None else total
    return LoadPinnedResponse(memories=items, total_available=total, truncated=total > cap, cap=cap)


@contextlib.contextmanager
def _patched(response):
    async def _get_db():
        yield AsyncMock()

    service = MagicMock(load_pinned=AsyncMock(return_value=response))
    with (
        patch("db.base.get_db", new=_get_db),
        patch(
            "mcp_server.tools.memory._resolve_context_for_read",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch("mcp_server.tools.memory._context_response_fields", return_value={}),
        patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()),
        patch("services.memory_service.MemoryService", new=MagicMock(return_value=service)),
    ):
        yield


async def _call(response, **args):
    with _patched(response):
        result = await handle_load_pinned({"context_id": str(uuid4()), **args}, "u", uuid4())
    return json.loads(result[0].text), result[0].text


@pytest.mark.asyncio
async def test_small_set_is_returned_whole():
    body, _ = await _call(_response(_items(3)))
    assert len(body["memories"]) == 3
    assert all(m["context_summary"] for m in body["memories"])
    assert body["truncated"] is False
    assert "context_summary_omitted" not in body


@pytest.mark.asyncio
async def test_context_summary_is_dropped_before_items():
    body, text = await _call(_response(_items(20)))
    assert len(text) <= 20_000
    assert body["context_summary_omitted"] is True
    assert len(body["memories"]) == 20
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_default_cap_worst_case_is_cut_and_flagged():
    body, text = await _call(_response(_items(100)))
    assert len(text) <= 20_000
    assert 0 < len(body["memories"]) < 100
    assert body["truncated"] is True
    assert body["total_available"] == 100


@pytest.mark.asyncio
async def test_max_chars_limit_fits_under_the_claude_ai_limit():
    body, text = await _call(_response(_items(100)), max_chars=100_000)
    assert len(text) <= 100_000
    assert len(body["memories"]) > 50


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [9_999, 100_001, "20000", True])
async def test_bad_max_chars_is_refused(bad):
    body, _ = await _call(_response([]), max_chars=bad)
    assert body["error"] == "validation_error"
