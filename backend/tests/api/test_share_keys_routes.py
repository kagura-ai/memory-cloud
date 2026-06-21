"""Route-logic tests for the share-key recall surface (#1027).

These call the ``share_recall`` route function directly with a fake share-key
principal and a mocked ``MemoryService`` — no DB, no ASGI client — to pin the
two confinement behaviours the gate-1 (CSO) review required:

- BOUND_SCOPE_VIOLATION: a client-supplied ``filters.context_id`` that differs
  from the key's bound context is rejected with 403.
- Forced confinement: recall is always issued against the BOUND context and
  the BOUND context's workspace, regardless of what the client requested
  (#963/#150 non-regression).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from api.routes.share_keys import share_recall
from models.schemas import RecallRequest

CTX = uuid.uuid4()
WS = uuid.uuid4()


def _principal() -> dict:
    return {
        "user_id": "owner-1",
        "role": "share-key",
        "current_context_id": CTX,
        "current_workspace_id": WS,
        "share_key_id": 7,
        "share_key_context_id": CTX,
        "scope": "memory:read",
    }


@pytest.mark.asyncio
async def test_mismatched_context_rejected_403() -> None:
    """A share key pointed at a different context → 403 BOUND_SCOPE_VIOLATION."""
    from utils.exceptions import AuthorizationError

    other = uuid.uuid4()
    request = RecallRequest(query="hi", filters={"context_id": str(other)})
    svc = AsyncMock()

    with pytest.raises(AuthorizationError) as exc:
        await share_recall(request=request, principal=_principal(), memory_service=svc)

    assert exc.value.status_code == 403
    assert "different context" in exc.value.message.lower()
    svc.recall.assert_not_awaited()  # never reaches the service


@pytest.mark.asyncio
async def test_malformed_context_id_maps_to_422() -> None:
    """A malformed (non-UUID) client context_id is a 422 (bad input), not a
    403 (scope violation) — the holder gets an accurate signal."""
    from utils.exceptions import ValidationError

    request = RecallRequest(query="hi", filters={"context_id": "not-a-uuid"})
    svc = AsyncMock()

    with pytest.raises(ValidationError) as exc:
        await share_recall(request=request, principal=_principal(), memory_service=svc)
    assert exc.value.status_code == 422
    svc.recall.assert_not_awaited()


@pytest.mark.asyncio
async def test_matching_context_confined_to_bound() -> None:
    """Matching context_id proceeds, forced to the bound context + workspace."""
    sentinel = object()
    svc = AsyncMock()
    svc.recall = AsyncMock(return_value=sentinel)
    request = RecallRequest(query="hi", filters={"context_id": str(CTX)})

    result = await share_recall(request=request, principal=_principal(), memory_service=svc)

    assert result is sentinel
    _, kwargs = svc.recall.call_args
    assert kwargs["current_context_id"] == CTX
    assert kwargs["current_workspace_id"] == WS
    assert kwargs["user_id"] == "owner-1"


@pytest.mark.asyncio
async def test_no_filters_still_confined_to_bound() -> None:
    """Even with no client context, recall is forced to the bound context."""
    sentinel = object()
    svc = AsyncMock()
    svc.recall = AsyncMock(return_value=sentinel)
    request = RecallRequest(query="hi")  # no filters at all

    result = await share_recall(request=request, principal=_principal(), memory_service=svc)

    assert result is sentinel
    _, kwargs = svc.recall.call_args
    assert kwargs["current_context_id"] == CTX
    assert kwargs["current_workspace_id"] == WS


@pytest.mark.asyncio
async def test_service_value_error_maps_to_422() -> None:
    from utils.exceptions import ValidationError

    svc = AsyncMock()
    svc.recall = AsyncMock(side_effect=ValueError("bad context"))
    request = RecallRequest(query="hi")

    with pytest.raises(ValidationError) as exc:
        await share_recall(request=request, principal=_principal(), memory_service=svc)
    assert exc.value.status_code == 422
