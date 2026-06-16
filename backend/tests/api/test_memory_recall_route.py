"""Route tests for POST /memory/recall (#1036).

Direct-call convention (like test_memory_pinned_route.py): invoke the handler
with a mocked MemoryService and assert argument plumbing.

Regression guard for #1036: the route used to hardcode current_context_id=None
while MemoryService.recall() requires a context, so every recall 500'd. The
route must forward filters["context_id"] as current_context_id.
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.memory import recall
from models.schemas import RecallRequest, RecallResponse

MOCK_USER = {"user_id": "u1", "current_workspace_id": uuid4()}


@pytest.mark.asyncio
async def test_recall_route_forwards_context_id_from_filters():
    """#1036: filters.context_id must reach the service as current_context_id."""
    svc = AsyncMock()
    svc.recall = AsyncMock(return_value=RecallResponse(results=[]))
    ctx = uuid4()
    req = RecallRequest(query="how does recall work?", k=3, filters={"context_id": str(ctx)})

    await recall(request=req, user=MOCK_USER, memory_service=svc)

    svc.recall.assert_awaited_once()
    kwargs = svc.recall.await_args.kwargs
    # Mirrors /remember: the raw string from the filter is forwarded as-is.
    assert kwargs["current_context_id"] == str(ctx)
    assert kwargs["current_workspace_id"] == MOCK_USER["current_workspace_id"]


@pytest.mark.asyncio
async def test_recall_route_forwards_none_when_no_filters():
    """No filters → current_context_id=None (the service guard then rejects it,
    same contract as before — see test_recall_no_workspace)."""
    svc = AsyncMock()
    svc.recall = AsyncMock(return_value=RecallResponse(results=[]))
    req = RecallRequest(query="test", k=5)

    await recall(request=req, user=MOCK_USER, memory_service=svc)

    kwargs = svc.recall.await_args.kwargs
    assert kwargs["current_context_id"] is None


@pytest.mark.asyncio
async def test_recall_route_maps_value_error_to_422():
    """A bad request (missing context, or a non-UUID context_id that fails to
    parse downstream) surfaces as 422, not an unhandled 500 — mirroring
    /remember and /pinned."""
    svc = AsyncMock()
    svc.recall = AsyncMock(
        side_effect=ValueError("recall() requires current_workspace_id and current_context_id")
    )
    req = RecallRequest(query="test", k=5, filters={"context_id": "not-a-uuid"})

    with pytest.raises(HTTPException) as exc:
        await recall(request=req, user=MOCK_USER, memory_service=svc)
    assert exc.value.status_code == 422
