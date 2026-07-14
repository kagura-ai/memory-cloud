"""Unit test: reference() surfaces the FULL ReferenceResponse (#1054).

Guards that handle_reference no longer drops scope/updated_at (#434), source
provenance (#215), or the declared-link references (#440) — the gap a first-time
agent hit when trying to see a memory's links/staleness via reference(). The
service already returns these; only the MCP handler's hand-built dict omitted
them. DB-backed service behaviour is covered elsewhere; here the service is mocked.
"""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from mcp_server.tools.memory import handle_reference
from models.schemas import LinkedMemoryRef, ReferenceResponse


def _payload(result):
    assert len(result) == 1
    return json.loads(result[0].text)


def _ref_response(mem_id, link_id):
    return ReferenceResponse(
        memory_id=mem_id,
        summary="seed summary",
        context_summary="ctx",
        content="full layer-3 content",
        details={"k": "v"},
        type="note",
        scope="working",
        importance=0.8,
        tags=["a"],
        context={"file_path": "x.py"},
        created_at=datetime(2026, 6, 1, 12, 0, 0),
        updated_at=datetime(2026, 6, 2, 9, 30, 0),
        client="api",
        source_uri="vault://v/note.md",
        source_type="vault",
        outgoing_links=[
            LinkedMemoryRef(
                memory_id=link_id,
                summary="linked",
                type="code",
                importance=0.5,
                weight=1.0,
                created_at=datetime(2026, 5, 1, 0, 0, 0),
            )
        ],
        outgoing_has_more=True,
        incoming_links=[],
        incoming_has_more=False,
    )


@pytest.mark.asyncio
async def test_reference_surfaces_links_scope_provenance():
    mem_id, link_id = uuid4(), uuid4()
    svc = MagicMock(reference=AsyncMock(return_value=_ref_response(mem_id, link_id)))

    async def gen():
        yield AsyncMock()

    with ExitStack() as stack:
        stack.enter_context(patch("db.base.get_db", new=gen))
        stack.enter_context(
            # last_used_at must be a real sentinel (not a MagicMock attribute):
            # #1257 touches the resolved context's timestamp with datetime math.
            patch(
                "mcp_server.tools.memory._resolve_context_for_read",
                new=AsyncMock(return_value=MagicMock(last_used_at=None)),
            )
        )
        stack.enter_context(patch("mcp_server.tools.memory._log_tool_usage", new=AsyncMock()))
        stack.enter_context(patch("services.memory_service.MemoryService", return_value=svc))
        result = await handle_reference(
            args={"memory_id": str(mem_id), "context_id": str(uuid4())},
            user_id="u",
            workspace_id=uuid4(),
        )

    m = _payload(result)["memory"]
    # Layer-3 basics still present
    assert m["memory_id"] == str(mem_id)
    assert m["content"] == "full layer-3 content"
    # #434: scope + updated_at (staleness cue) — previously dropped
    assert m["scope"] == "working"
    assert m["updated_at"].startswith("2026-06-02")
    # #215: provenance — previously dropped
    assert m["source_uri"] == "vault://v/note.md"
    assert m["source_type"] == "vault"
    # #440: declared-link references — previously dropped
    assert m["outgoing_links"][0]["memory_id"] == str(link_id)
    assert m["outgoing_has_more"] is True
    assert m["incoming_links"] == []
    assert m["incoming_has_more"] is False
