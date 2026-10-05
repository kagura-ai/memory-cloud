"""Route tests for POST /memory/recall (#1036).

Direct-call convention (like test_memory_pinned_route.py): invoke the handler
with a mocked MemoryService and assert argument plumbing.

Regression guard for #1036: the route used to hardcode current_context_id=None
while MemoryService.recall() requires a context, so every recall 500'd. The
route must forward filters["context_id"] as current_context_id.

The route resolves ``filters.context_id`` through
``PermissionService.resolve_context_for_workspace_read`` before the service
runs — the same gate as ``/memory/list``, ``/memory/stats`` and the MCP
``recall`` tool — so a context the caller cannot open is the uniform 404 and
the search is scoped to the context's own workspace.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.memory import recall
from models.schemas import RecallRequest, RecallResponse
from utils.exceptions import NotFoundException

MOCK_USER = {"user_id": "u1", "current_workspace_id": uuid4()}


def _resolver(context=None, *, error: Exception | None = None):
    """Patch the route's PermissionService; returns the resolve mock."""
    resolve = AsyncMock(return_value=context, side_effect=error)
    patcher = patch(
        "api.routes.memory.PermissionService",
        return_value=MagicMock(resolve_context_for_workspace_read=resolve),
    )
    return patcher, resolve


@pytest.mark.asyncio
async def test_recall_route_forwards_context_id_from_filters():
    """#1036: filters.context_id must reach the service as current_context_id —
    as the UUID of the context the resolver returned, with the context's own
    workspace as the paying / search workspace (the MCP handler's plumbing)."""
    svc = AsyncMock()
    svc.recall = AsyncMock(return_value=RecallResponse(results=[]))
    ctx = uuid4()
    context = SimpleNamespace(id=ctx, workspace_id=uuid4(), is_private=True)
    req = RecallRequest(query="how does recall work?", k=3, filters={"context_id": str(ctx)})
    patcher, resolve = _resolver(context)

    with patcher:
        await recall(request=req, user=MOCK_USER, memory_service=svc, db=MagicMock())

    svc.recall.assert_awaited_once()
    kwargs = svc.recall.await_args.kwargs
    assert kwargs["current_context_id"] == ctx
    assert kwargs["current_workspace_id"] == MOCK_USER["current_workspace_id"]
    assert kwargs["context_workspace_id"] == context.workspace_id
    resolve.assert_awaited_once()
    assert resolve.await_args.kwargs["user_id"] == "u1"
    assert resolve.await_args.kwargs["context_id"] == ctx


@pytest.mark.asyncio
async def test_recall_route_confines_a_workspace_scoped_key():
    """A workspace-scoped API key's pure key scope reaches the resolver
    (#963), like every other UUID-addressed read."""
    svc = AsyncMock()
    svc.recall = AsyncMock(return_value=RecallResponse(results=[]))
    ctx = uuid4()
    key_ws = uuid4()
    user = {**MOCK_USER, "api_key_workspace_id": key_ws}
    req = RecallRequest(query="q", k=3, filters={"context_id": str(ctx)})
    patcher, resolve = _resolver(SimpleNamespace(id=ctx, workspace_id=key_ws))

    with patcher:
        await recall(request=req, user=user, memory_service=svc, db=MagicMock())

    assert resolve.await_args.kwargs["key_workspace_id"] == key_ws


@pytest.mark.asyncio
async def test_recall_route_forwards_none_when_no_filters():
    """No filters → current_context_id=None (the service guard then rejects it,
    same contract as before — see test_recall_no_workspace). Nothing to
    resolve, so the permission service is not consulted."""
    svc = AsyncMock()
    svc.recall = AsyncMock(return_value=RecallResponse(results=[]))
    req = RecallRequest(query="test", k=5)
    patcher, resolve = _resolver()

    with patcher:
        await recall(request=req, user=MOCK_USER, memory_service=svc, db=MagicMock())

    kwargs = svc.recall.await_args.kwargs
    assert kwargs["current_context_id"] is None
    assert kwargs["context_workspace_id"] is None
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_recall_route_maps_value_error_to_422():
    """A bad request (missing context) surfaces as 422, not an unhandled 500 —
    mirroring /remember and /pinned."""
    svc = AsyncMock()
    svc.recall = AsyncMock(
        side_effect=ValueError("recall() requires current_workspace_id and current_context_id")
    )
    req = RecallRequest(query="test", k=5)
    patcher, _ = _resolver()

    with patcher, pytest.raises(HTTPException) as exc:
        await recall(request=req, user=MOCK_USER, memory_service=svc, db=MagicMock())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_recall_route_rejects_a_non_uuid_context_id_before_any_lookup():
    """A non-UUID context_id is a 422 at the boundary (like /pinned) — it never
    reaches the resolver or the service."""
    svc = AsyncMock()
    svc.recall = AsyncMock(return_value=RecallResponse(results=[]))
    req = RecallRequest(query="test", k=5, filters={"context_id": "not-a-uuid"})
    patcher, resolve = _resolver()

    with patcher, pytest.raises(HTTPException) as exc:
        await recall(request=req, user=MOCK_USER, memory_service=svc, db=MagicMock())

    assert exc.value.status_code == 422
    resolve.assert_not_awaited()
    svc.recall.assert_not_awaited()


@pytest.mark.asyncio
async def test_recall_route_denied_context_is_the_uniform_404_and_never_searches():
    """Unknown, other-workspace, private non-creator, suspended or whitelist-
    excluded: the resolver's NotFoundException propagates unchanged (the global
    handler renders the 404) and the service is never called."""
    svc = AsyncMock()
    svc.recall = AsyncMock(return_value=RecallResponse(results=[]))
    ctx = uuid4()
    req = RecallRequest(query="test", k=5, filters={"context_id": str(ctx)})
    patcher, _ = _resolver(error=NotFoundException("Context", str(ctx)))

    with patcher, pytest.raises(NotFoundException):
        await recall(request=req, user=MOCK_USER, memory_service=svc, db=MagicMock())

    svc.recall.assert_not_awaited()
