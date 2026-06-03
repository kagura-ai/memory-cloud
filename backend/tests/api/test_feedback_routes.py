"""Route-surface tests for the retrieval feedback endpoint (Issue #888).

Pins the REST wiring around a mocked FeedbackService + mocked PermissionService
(persistence is integration-tested separately). Load-bearing contracts:
- feedback is gated on the READ path (check_context_access VIEWER), not write —
  recording feedback is read-adjacent;
- a gate denial propagates as the structured MemoryCloudException (404/403),
  not swallowed into a 500;
- the response envelope carries the new feedback id + the rated memory + verdict.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.routes.feedback import FeedbackRequest, record_feedback
from auth.workspace_roles import ContextRole
from utils.exceptions import AuthorizationError, NotFoundException

MOCK_USER = {"user_id": "test_user"}


@pytest.fixture
def context_id():
    return uuid4()


@pytest.fixture
def memory_id():
    return uuid4()


@pytest.fixture
def service(memory_id):
    row = MagicMock(id=uuid4(), memory_id=memory_id, helpful=True)
    return MagicMock(record_feedback=AsyncMock(return_value=row))


@pytest.fixture
def perm():
    return MagicMock(check_context_access=AsyncMock())


@pytest.mark.asyncio
async def test_record_feedback_success_uses_read_gate(service, perm, context_id, memory_id):
    body = FeedbackRequest(memory_id=memory_id, helpful=True, query="how does recall work")
    resp = await record_feedback(
        context_id=context_id, body=body, user=MOCK_USER, service=service, perm=perm
    )
    assert resp.memory_id == memory_id
    assert resp.helpful is True
    perm.check_context_access.assert_awaited_once_with(
        "test_user", context_id, required_role=ContextRole.VIEWER
    )
    # user_id is taken from the authenticated principal, not the body.
    service.record_feedback.assert_awaited_once()
    assert service.record_feedback.await_args.kwargs["user_id"] == "test_user"


@pytest.mark.asyncio
async def test_record_feedback_unreachable_context_propagates_404(
    service, perm, context_id, memory_id
):
    perm.check_context_access.side_effect = NotFoundException("Context")
    with pytest.raises(NotFoundException):
        await record_feedback(
            context_id=context_id,
            body=FeedbackRequest(memory_id=memory_id, helpful=False),
            user=MOCK_USER,
            service=service,
            perm=perm,
        )
    service.record_feedback.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_feedback_denied_propagates_403(service, perm, context_id, memory_id):
    perm.check_context_access.side_effect = AuthorizationError()
    with pytest.raises(AuthorizationError):
        await record_feedback(
            context_id=context_id,
            body=FeedbackRequest(memory_id=memory_id, helpful=True),
            user=MOCK_USER,
            service=service,
            perm=perm,
        )
    service.record_feedback.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_feedback_wraps_unexpected_error_as_500(service, perm, context_id, memory_id):
    service.record_feedback.side_effect = RuntimeError("db down")
    with pytest.raises(HTTPException) as exc:
        await record_feedback(
            context_id=context_id,
            body=FeedbackRequest(memory_id=memory_id, helpful=True),
            user=MOCK_USER,
            service=service,
            perm=perm,
        )
    assert exc.value.status_code == 500


def test_overlong_query_rejected_by_schema(memory_id):
    with pytest.raises(ValueError):  # pydantic ValidationError subclasses ValueError
        FeedbackRequest(memory_id=memory_id, helpful=True, query="x" * 1025)


def test_overlong_note_rejected_by_schema(memory_id):
    # note > 2000 chars is a 422 at the schema boundary, NOT silent truncation.
    with pytest.raises(ValueError):
        FeedbackRequest(memory_id=memory_id, helpful=True, note="y" * 2001)


@pytest.mark.asyncio
async def test_mcp_handle_feedback_rejects_overlong_note():
    """The MCP tool rejects an overlong note with a structured validation error,
    before touching the DB (no silent truncation)."""
    from mcp_server.tools.feedback import handle_feedback

    result = await handle_feedback(
        {
            "context_id": str(uuid4()),
            "memory_id": str(uuid4()),
            "helpful": True,
            "note": "y" * 2001,
        },
        user_id="test_user",
        workspace_id=None,
    )
    # _error_response returns a single TextContent whose text carries the error.
    assert "validation_error" in result[0].text
