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
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from api.routes.share_keys import share_recall, share_sessions
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


# ---------------------------------------------------------------------------
# share_sessions (#1064) — read-only observation, confined + projected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_share_sessions_confined_and_projected() -> None:
    """Lists ONLY the bound context's state and projects status/awaiting_approval."""
    ts = datetime(2026, 6, 21, 12, 0, 0)
    svc = AsyncMock()
    svc.list_state_detail = AsyncMock(
        return_value=[
            {
                "key": "thread-1",
                "value": {"status": "awaiting_approval", "caps": ["x"]},
                "updated_at": ts,
            },
            {"key": "thread-2", "value": {"status": "running"}, "updated_at": ts},
            {"key": "thread-3", "value": "scalar-not-a-dict", "updated_at": ts},
        ]
    )

    resp = await share_sessions(principal=_principal(), agent_state_service=svc)

    # Confinement: queried with the bound context, never a client-supplied one.
    svc.list_state_detail.assert_awaited_once_with(CTX)
    assert resp.count == 3
    by_key = {s.key: s for s in resp.sessions}
    assert by_key["thread-1"].status == "awaiting_approval"
    assert by_key["thread-1"].awaiting_approval is True
    assert by_key["thread-2"].status == "running"
    assert by_key["thread-2"].awaiting_approval is False
    # Non-dict value must not crash; status is null and not awaiting.
    assert by_key["thread-3"].status is None
    assert by_key["thread-3"].awaiting_approval is False


@pytest.mark.asyncio
async def test_share_sessions_empty() -> None:
    svc = AsyncMock()
    svc.list_state_detail = AsyncMock(return_value=[])
    resp = await share_sessions(principal=_principal(), agent_state_service=svc)
    assert resp.count == 0
    assert resp.sessions == []


def test_share_surface_is_read_only() -> None:
    """Permission boundary (#1064 AC): the share-key surface (/api/v1/share/*)
    exposes ONLY read verbs — no PUT/PATCH/DELETE — so a share key can never
    reach a write/control operation. Write routes live elsewhere behind
    api-key/session auth, where a share key is structurally rejected."""
    from api.main import app

    share_methods: set[str] = set()
    share_paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/v1/share/"):
            share_paths.add(path)
            share_methods |= {
                m
                for m in (getattr(route, "methods", None) or set())
                if m != "HEAD" and m != "OPTIONS"
            }

    assert share_paths == {"/api/v1/share/recall", "/api/v1/share/sessions"}
    assert share_methods <= {"GET", "POST"}
    assert not (share_methods & {"PUT", "PATCH", "DELETE"})
