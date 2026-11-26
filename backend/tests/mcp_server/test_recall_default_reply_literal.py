"""The default recall reply, pinned against the code before #3004.

``order`` is opt-in: omitted (or ``"relevance"``) the MCP reply, the REST
body and the explore body (which serializes ``MemoryResponse`` without
``exclude_none``) must be exactly what they were. The literals below were
produced by running this file's fixture (the same rows, the same stubs) with
the code at origin/main d00a3c64, in a temporary detached worktree of that
commit, through a one-off script that printed ``handle_recall``'s text, the
REST route's body and the explore body; they were pasted here unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.memory import recall as rest_recall
from mcp_server.tools._helpers import set_mcp_tool_view_query
from mcp_server.tools.memory import handle_recall
from models.schemas import (
    ExploreResponse,
    MemoryResponse,
    RecallConfidence,
    RecallRequest,
    RecallResponse,
)

MCP_COMPACT = (
    '{"confidence":{"level":"moderate","top_score":0.6,"prominence":null,"relative_ma'
    'rgin":null,"result_count":3,"rationale":"r"},"status":"success","results":[{"mem'
    'ory_id":"00000000-0000-0000-0000-000000000003","summary":"summary 3","content":"'
    'content three","type":"fact","importance":0.5,"score":0.6,"created_at":"2026-09-'
    '03T09:00:00Z","updated_at":null},{"memory_id":"00000000-0000-0000-0000-000000000'
    '001","summary":"summary 1","type":"fact","importance":0.5,"score":0.8,"created_a'
    't":"2026-09-01T09:00:00Z","updated_at":null,"observed_at":"2026-08-01","observed'
    '_weekday":"Sat","superseded_by":"00000000-0000-0000-0000-000000000003"},{"memory'
    '_id":"00000000-0000-0000-0000-000000000002","summary":"summary 2","type":"fact",'
    '"importance":0.5,"score":0.7,"created_at":"2026-09-02T09:00:00Z","updated_at":nu'
    'll,"event_start":"2026-07-04","event_end":"2026-07-05","event_weekday":"Sat"}],"'
    'count":3}'
)

MCP_FULL = (
    '{"confidence":{"level":"moderate","top_score":0.6,"prominence":null,"relative_ma'
    'rgin":null,"result_count":3,"rationale":"r"},"status":"success","results":[{"mem'
    'ory_id":"00000000-0000-0000-0000-000000000003","summary":"summary 3","content":"'
    'content three","context_summary":"why 3","type":"fact","importance":0.5,"scope":'
    '"persistent","score":0.6,"created_at":"2026-09-03T09:00:00Z","updated_at":null,"'
    'version":2,"tags":["t"]},{"memory_id":"00000000-0000-0000-0000-000000000001","su'
    'mmary":"summary 1","context_summary":"why 1","type":"fact","importance":0.5,"sco'
    'pe":"persistent","score":0.8,"created_at":"2026-09-01T09:00:00Z","updated_at":nu'
    'll,"observed_at":"2026-08-01","observed_weekday":"Sat","tags":["t"],"superseded_'
    'by":"00000000-0000-0000-0000-000000000003"},{"memory_id":"00000000-0000-0000-000'
    '0-000000000002","summary":"summary 2","context_summary":"why 2","type":"fact","i'
    'mportance":0.5,"scope":"persistent","score":0.7,"created_at":"2026-09-02T09:00:0'
    '0Z","updated_at":null,"event_start":"2026-07-04","event_end":"2026-07-05","event'
    '_weekday":"Sat"}],"count":3}'
)

REST_BODY = (
    '{"results":[{"memory_id":"00000000-0000-0000-0000-000000000003","summary":"summa'
    'ry 3","context_summary":"why 3","type":"fact","importance":0.5,"scope":"persiste'
    'nt","created_at":"2026-09-03T09:00:00Z","client":"mcp","tags":["t"],"score":0.60'
    '00000000000001,"contradicts":[],"version":2,"content":"content three"},{"memory_'
    'id":"00000000-0000-0000-0000-000000000001","summary":"summary 1","context_summar'
    'y":"why 1","type":"fact","importance":0.5,"scope":"persistent","created_at":"202'
    '6-09-01T09:00:00Z","observed_at":"2026-08-01","client":"mcp","tags":["t"],"score'
    '":0.8,"superseded_by":"00000000-0000-0000-0000-000000000003","contradicts":[]},{'
    '"memory_id":"00000000-0000-0000-0000-000000000002","summary":"summary 2","contex'
    't_summary":"why 2","type":"fact","importance":0.5,"scope":"persistent","created_'
    'at":"2026-09-02T09:00:00Z","client":"mcp","tags":[],"score":0.7,"contradicts":[]'
    '}],"related_tags":[],"confidence":{"level":"moderate","top_score":0.6,"result_co'
    'unt":3,"rationale":"r"}}'
)

EXPLORE_BODY = (
    '{"seed_memory":{"memory_id":"00000000-0000-0000-0000-000000000001","summary":"su'
    'mmary 1","context_summary":"why 1","type":"fact","importance":0.5,"scope":"persi'
    'stent","created_at":"2026-09-01T09:00:00Z","updated_at":null,"observed_at":"2026'
    '-08-01","client":"mcp","tags":["t"],"context":null,"score":0.8,"source_uri":null'
    ',"source_type":null,"superseded_by":"00000000-0000-0000-0000-000000000003","cont'
    'radicts":[],"supersede_candidate":null,"folded":null,"version":null,"content":nu'
    'll,"content_truncated":null,"content_next_offset":null,"spans":null},"related_me'
    'mories":[],"metadata":{}}'
)


def _rows() -> list[MemoryResponse]:
    def row(n: int, **kw: Any) -> MemoryResponse:
        base: dict[str, Any] = {
            "memory_id": UUID(int=n),
            "summary": f"summary {n}",
            "context_summary": f"why {n}",
            "type": "fact",
            "importance": 0.5,
            "scope": "persistent",
            "created_at": datetime(2026, 9, n, 9, 0, tzinfo=UTC),
            "updated_at": None,
            "client": "mcp",
            "tags": ["t"],
            "context": None,
            "score": 0.9 - n / 10,
        }
        base.update(kw)
        return MemoryResponse(**base)

    return [
        row(3, content="content three", version=2),
        row(1, observed_at="2026-08-01", superseded_by=UUID(int=3)),
        row(2, event_start="2026-07-04", event_end="2026-07-05", tags=[]),
    ]


def _response() -> RecallResponse:
    return RecallResponse(
        results=_rows(),
        confidence=RecallConfidence(level="moderate", top_score=0.6, result_count=3, rationale="r"),
    )


@pytest.fixture(autouse=True)
def _reset_view():
    set_mcp_tool_view_query(None)
    yield
    set_mcp_tool_view_query(None)


async def _mcp(args: dict[str, Any]) -> str:
    db = AsyncMock()

    async def fake_get_db():
        yield db

    async def fake_recall(request: RecallRequest, **_kw: Any) -> RecallResponse:
        return _response()

    service = MagicMock(recall=fake_recall, record_delivery=AsyncMock(return_value=True))
    context = MagicMock(trust_tier="trusted", workspace_id=UUID(int=9), is_private=False)
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
    ):
        out = await handle_recall(
            {"query": "q", "context_id": str(UUID(int=7)), **args}, user_id="u1", workspace_id=None
        )
    return out[0].text


def _rest_client() -> TestClient:
    app = FastAPI()

    @app.post("/r", response_model=RecallResponse, response_model_exclude_none=True)
    async def route(request: RecallRequest) -> RecallResponse:
        svc = AsyncMock()
        svc.recall = AsyncMock(return_value=_response())
        context = SimpleNamespace(id=UUID(int=7), workspace_id=UUID(int=9), is_private=False)
        with patch(
            "api.routes.memory.PermissionService",
            return_value=MagicMock(
                resolve_context_for_workspace_read=AsyncMock(return_value=context)
            ),
        ):
            return await rest_recall(
                request=request,
                user={"user_id": "u1", "current_workspace_id": UUID(int=9)},
                memory_service=svc,
                db=MagicMock(),
            )

    return TestClient(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{}, {"order": "relevance"}, {"order": None}])
@pytest.mark.parametrize(("verbosity", "expected"), [("compact", MCP_COMPACT), ("full", MCP_FULL)])
async def test_mcp_reply_matches_the_base(extra, verbosity, expected):
    assert await _mcp({"verbosity": verbosity, **extra}) == expected


@pytest.mark.parametrize("extra", [{}, {"order": "relevance"}])
def test_rest_body_matches_the_base(extra):
    body = {"query": "q", "filters": {"context_id": str(UUID(int=7))}, **extra}
    assert _rest_client().post("/r", json=body).text == REST_BODY


def test_explore_body_matches_the_base():
    app = FastAPI()

    @app.get("/e", response_model=ExploreResponse)
    async def route() -> ExploreResponse:
        return ExploreResponse(seed_memory=_rows()[1], related_memories=[])

    assert TestClient(app).get("/e").text == EXPLORE_BODY
