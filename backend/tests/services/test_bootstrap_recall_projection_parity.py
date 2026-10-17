"""One recall result projection for recall() and get_agent_bootstrap (#2013).

The bootstrap recall component used to hand-build its rows: ``context_summary:
null``, ``superseded_by: null``, ``contradicts: []``, ``tags: []`` and an
unrounded ``score`` — while ``recall`` omits an empty annotation and rounds the
score (#1599, #2002). The same memory came back in two shapes. Both paths now
project through ``utils.recall_projection.recall_result_item``; these tests
feed one memory through each and compare the rows.

One documented exception: bootstrap rows never carry ``supersede_candidate``
(recall does). Bootstrap is the behaviour-establishing session-start lane —
an agent does not act on a suggestion there, the field cannot be dropped by
the max_chars fit, and it would put another memory's summary in the trusted
lane.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_server.tools import memory as mcp_memory
from models.schemas import MemoryResponse, RecallResponse, SupersedeCandidate
from services.agent_bootstrap_service import (
    AgentBootstrapService,
    BootstrapParams,
    BootstrapPrincipal,
)

WORKSPACE_ID = uuid.uuid4()
CONTEXT_ID = uuid.uuid4()


def _memory(**overrides) -> MemoryResponse:
    fields = {
        "memory_id": uuid.uuid4(),
        "summary": "JWT expiry caused 401; fixed with refresh token rotation",
        "context_summary": None,
        "type": "bug-fix",
        "importance": 0.8,
        "scope": "persistent",
        "created_at": datetime(2026, 8, 1, 9, 30, tzinfo=UTC),
        "updated_at": None,
        "client": "mcp",
        "tags": [],
        "context": None,
        "score": 0.8287090031354203,
    }
    fields.update(overrides)
    return MemoryResponse(**fields)


def _context():
    context = MagicMock()
    context.id = CONTEXT_ID
    context.workspace_id = WORKSPACE_ID
    context.name = "dev"
    context.display_name = "Development"
    context.is_private = False
    context.is_locked = False
    return context


def _recall_tool_row(memory: MemoryResponse, verbosity: str) -> dict:
    envelope = mcp_memory._recall_envelope(RecallResponse(results=[memory]), _context(), verbosity)
    return envelope["results"][0]


async def _bootstrap_row(memory: MemoryResponse) -> dict:
    svc = AgentBootstrapService(MagicMock())
    with patch("services.memory_service.MemoryService") as ms_cls:
        ms_cls.return_value.recall = AsyncMock(return_value=RecallResponse(results=[memory]))
        body = await svc._recall(
            _context(),
            BootstrapParams(agent_id=uuid.uuid4(), query="auth 401"),
            BootstrapPrincipal(user_id="u", workspace_id=WORKSPACE_ID, principal_type="agent"),
        )
    return body["results"][0]


@pytest.mark.asyncio
class TestBootstrapRecallRowMatchesRecall:
    async def test_a_bare_memory_has_the_same_keys_and_no_null_annotations(self):
        memory = _memory()

        row = await _bootstrap_row(memory)

        assert row == _recall_tool_row(memory, "full")
        assert list(row) == list(_recall_tool_row(memory, "full"))
        # Absence is the signal on both paths (#1599): never null / [].
        for key in ("context_summary", "superseded_by", "contradicts", "tags"):
            assert key not in row
        assert row["score"] == 0.8287

    async def test_an_annotated_memory_carries_the_same_values_but_no_suggestion(self):
        memory = _memory(
            context_summary="Recall when auth starts failing after a deploy",
            tags=["auth", "jwt"],
            updated_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
            superseded_by=uuid.uuid4(),
            contradicts=[uuid.uuid4()],
            supersede_candidate=SupersedeCandidate(
                memory_id=uuid.uuid4(), similarity=0.93, summary="older note"
            ),
        )

        row = await _bootstrap_row(memory)
        recall_row = _recall_tool_row(memory, "full")

        # The one exception to key-set parity: recall keeps the suggestion,
        # bootstrap never carries it.
        assert "supersede_candidate" in recall_row
        assert "supersede_candidate" not in row
        expected = {k: v for k, v in recall_row.items() if k != "supersede_candidate"}
        assert row == expected
        assert list(row) == list(expected)
        assert row["contradicts"] == [str(c) for c in memory.contradicts]

    async def test_bootstrap_serves_the_full_projection(self):
        # Bootstrap rows are behaviour-establishing and read once at session
        # start: the whole summary, context_summary, scope and tags — not the
        # 160-character compact head recall defaults to.
        memory = _memory(summary="x " * 200, tags=["a"])

        row = await _bootstrap_row(memory)

        assert row == _recall_tool_row(memory, "full")
        assert row != _recall_tool_row(memory, "compact")
        assert "summary_cut" not in row
