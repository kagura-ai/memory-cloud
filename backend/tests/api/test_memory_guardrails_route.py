"""Route tests for POST /memory/guardrails — the REST twin of MCP ``load_guardrails``.

Direct-call convention (like ``test_memory_pinned_route.py``): invoke the
handler with a mocked MemoryService, asserting argument plumbing (context UUID
parse, cap, pure key scope), the ValueError -> 422 map, and that MCP and REST
serve the same service result (parity by construction: one service method).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.memory import load_guardrails
from models.schemas import GuardrailItem, LoadGuardrailsRequest, LoadGuardrailsResponse

MOCK_USER = {"user_id": "u1", "current_workspace_id": uuid4()}
NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
TT = {"tool": "Bash|PowerShell", "on": "pre", "match": "gh pr merge", "action": "inform"}


def _item(tool_trigger=None, l2=None):
    return GuardrailItem(
        memory_id=uuid4(),
        summary="safe alternative, stated as a fact",
        context_summary=l2,
        type="troubleshooting",
        importance=0.8,
        delivery_mode="on_recall",
        tool_trigger=tool_trigger,
        source_type="manual",
        authored_by_caller=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _response(pinned=1, tool=1):
    return LoadGuardrailsResponse(
        format=1,
        version="3f9c1a7b2d4e6f80",
        pinned=[_item(l2="why") for _ in range(pinned)],
        tool_triggered=[_item(tool_trigger=dict(TT)) for _ in range(tool)],
        total_available=pinned + tool,
        truncated=False,
        cap=50,
        pinned_cap=100,
        pinned_total_available=pinned,
        pinned_truncated=False,
        tool_triggered_total_available=tool,
        tool_triggered_truncated=False,
    )


@pytest.mark.asyncio
async def test_route_passes_context_and_cap_through():
    svc = AsyncMock()
    svc.load_guardrails = AsyncMock(return_value=_response(pinned=2, tool=3))
    ctx = uuid4()
    req = LoadGuardrailsRequest(context_id=str(ctx), cap=25)

    result = await load_guardrails(request=req, user=MOCK_USER, memory_service=svc)

    assert len(result.pinned) == 2 and len(result.tool_triggered) == 3
    assert result.format == 1 and result.version == "3f9c1a7b2d4e6f80"
    kwargs = svc.load_guardrails.await_args.kwargs
    assert kwargs["current_context_id"] == ctx  # parsed to a UUID before forwarding
    assert kwargs["cap"] == 25
    assert kwargs["current_workspace_id"] == MOCK_USER["current_workspace_id"]
    assert kwargs["key_workspace_id"] is None


@pytest.mark.asyncio
async def test_route_forwards_pure_api_key_workspace_scope():
    svc = AsyncMock()
    svc.load_guardrails = AsyncMock(return_value=_response())
    key_ws = uuid4()
    user = {"user_id": "u1", "current_workspace_id": uuid4(), "api_key_workspace_id": key_ws}

    await load_guardrails(
        request=LoadGuardrailsRequest(context_id=str(uuid4())), user=user, memory_service=svc
    )

    kwargs = svc.load_guardrails.await_args.kwargs
    assert kwargs["key_workspace_id"] == key_ws
    assert kwargs["key_workspace_id"] != user["current_workspace_id"]


@pytest.mark.asyncio
async def test_route_rejects_malformed_context_id_with_422():
    svc = AsyncMock()
    svc.load_guardrails = AsyncMock(return_value=_response())
    with pytest.raises(HTTPException) as exc:
        await load_guardrails(
            request=LoadGuardrailsRequest(context_id="not-a-uuid"),
            user=MOCK_USER,
            memory_service=svc,
        )
    assert exc.value.status_code == 422
    svc.load_guardrails.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_maps_value_error_to_422():
    svc = AsyncMock()
    svc.load_guardrails = AsyncMock(
        side_effect=ValueError("load_guardrails() requires current_context_id")
    )
    with pytest.raises(HTTPException) as exc:
        await load_guardrails(
            request=LoadGuardrailsRequest(context_id=None), user=MOCK_USER, memory_service=svc
        )
    assert exc.value.status_code == 422


def test_request_cap_is_bounded_like_pinned():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LoadGuardrailsRequest(cap=0)
    with pytest.raises(ValidationError):
        LoadGuardrailsRequest(cap=1001)
    assert LoadGuardrailsRequest(cap=1000).cap == 1000


def test_rest_body_matches_the_mcp_envelope_fields():
    """MCP and REST return the same result: the MCP handler projects exactly
    the response-model fields (plus context_* which REST carries via the
    request). Pin the field set so the two surfaces cannot drift apart."""
    body = _response().model_dump()
    assert set(body) == {
        "status",
        "format",
        "version",
        "pinned",
        "tool_triggered",
        "total_available",
        "truncated",
        "cap",
        "pinned_cap",
        "pinned_total_available",
        "pinned_truncated",
        "tool_triggered_total_available",
        "tool_triggered_truncated",
    }
    item = body["tool_triggered"][0]
    assert set(item) == {
        "memory_id",
        "summary",
        "context_summary",
        "type",
        "importance",
        "delivery_mode",
        "tool_trigger",
        "source_type",
        "authored_by_caller",
        "created_at",
        "updated_at",
    }
    # The wire form serializes timestamps with an explicit UTC Z (TZAwareBaseModel).
    assert _response().model_dump(mode="json")["pinned"][0]["created_at"].endswith("Z")
