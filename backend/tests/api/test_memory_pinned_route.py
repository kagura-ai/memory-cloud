"""Route tests for POST /memory/pinned (#886).

Direct-call convention (like test_memory_list_time.py): invoke the handler with
a mocked MemoryService, asserting argument plumbing and the ValueError→422 map.
"""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.memory import load_pinned
from models.schemas import LoadPinnedRequest, LoadPinnedResponse, PinnedMemoryItem
from utils.datetime import utcnow

MOCK_USER = {"user_id": "u1", "current_workspace_id": uuid4()}


def _response(n=1, total=1, truncated=False, cap=100):
    return LoadPinnedResponse(
        memories=[
            PinnedMemoryItem(
                memory_id=uuid4(),
                summary=f"goal {i}",
                context_summary="why",
                type="note",
                importance=0.9,
                delivery_mode="always",
                created_at=utcnow(),
            )
            for i in range(n)
        ],
        total_available=total,
        truncated=truncated,
        cap=cap,
    )


@pytest.mark.asyncio
async def test_load_pinned_route_passes_context_and_cap_through():
    svc = AsyncMock()
    svc.load_pinned = AsyncMock(return_value=_response(n=2, total=2))
    ctx = uuid4()
    req = LoadPinnedRequest(context_id=str(ctx), cap=50)

    result = await load_pinned(request=req, user=MOCK_USER, memory_service=svc)

    assert len(result.memories) == 2
    svc.load_pinned.assert_awaited_once()
    kwargs = svc.load_pinned.await_args.kwargs
    # Route parses the string into a UUID before forwarding.
    assert kwargs["current_context_id"] == ctx
    assert kwargs["cap"] == 50
    assert kwargs["current_workspace_id"] == MOCK_USER["current_workspace_id"]


@pytest.mark.asyncio
async def test_load_pinned_route_forwards_pure_api_key_workspace_scope():
    """Issue #963/#1281 item 2: the route forwards the PURE key scope
    (api_key_workspace_id), NOT current_workspace_id, as key_workspace_id — the
    distinction that avoids over-confining OAuth/session/global-key callers."""
    svc = AsyncMock()
    svc.load_pinned = AsyncMock(return_value=_response())
    key_ws = uuid4()
    user = {"user_id": "u1", "current_workspace_id": uuid4(), "api_key_workspace_id": key_ws}
    req = LoadPinnedRequest(context_id=str(uuid4()), cap=10)

    await load_pinned(request=req, user=user, memory_service=svc)

    kwargs = svc.load_pinned.await_args.kwargs
    assert kwargs["key_workspace_id"] == key_ws
    assert kwargs["key_workspace_id"] != user["current_workspace_id"]


@pytest.mark.asyncio
async def test_load_pinned_route_rejects_malformed_context_id_with_422():
    """A non-UUID context_id is a 422, not a DB DataError → 503 (finding #3)."""
    svc = AsyncMock()
    svc.load_pinned = AsyncMock(return_value=_response())
    req = LoadPinnedRequest(context_id="not-a-uuid")

    with pytest.raises(HTTPException) as exc:
        await load_pinned(request=req, user=MOCK_USER, memory_service=svc)
    assert exc.value.status_code == 422
    svc.load_pinned.assert_not_awaited()  # rejected before reaching the service


@pytest.mark.asyncio
async def test_load_pinned_route_maps_value_error_to_422():
    svc = AsyncMock()
    svc.load_pinned = AsyncMock(side_effect=ValueError("load_pinned() requires current_context_id"))
    req = LoadPinnedRequest(context_id=None)

    with pytest.raises(HTTPException) as exc:
        await load_pinned(request=req, user=MOCK_USER, memory_service=svc)
    assert exc.value.status_code == 422
