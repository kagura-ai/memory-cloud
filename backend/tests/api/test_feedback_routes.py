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

from api.routes.feedback import (
    FeedbackRequest,
    HostFeedbackRequest,
    record_feedback,
    record_host_feedback,
    require_host_feedback_operator,
)
from auth.agent_scope import AgentScope, set_agent_scope
from auth.workspace_roles import ContextRole
from models.agent import AGENT_ENFORCEMENT_ENFORCE
from utils.exceptions import AuthorizationError, InternalError, NotFoundException

MOCK_USER = {"user_id": "test_user"}
OPERATOR_WORKSPACE_ID = uuid4()
OPERATOR_USER = {
    "user_id": "operator",
    "email": "operator@example.com",
    "current_workspace_id": OPERATOR_WORKSPACE_ID,
    "api_key_workspace_id": OPERATOR_WORKSPACE_ID,
    "api_key_prefix": "kagura_operator",
}


@pytest.fixture
def context_id():
    return uuid4()


@pytest.fixture
def memory_id():
    return uuid4()


@pytest.fixture
def service(memory_id):
    row = MagicMock(id=uuid4(), memory_id=memory_id, helpful=True)
    return MagicMock(
        record_feedback=AsyncMock(return_value=row),
        record_host_feedback=AsyncMock(return_value=row),
    )


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
        "test_user",
        context_id,
        required_role=ContextRole.VIEWER,
        # #1286 (P0-5): deny-capture audit identity — a binding/rbac deny at
        # this gate persists an operation="feedback" row.
        operation="feedback",
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


class TestHostFeedbackRoute:
    def test_public_and_host_schemas_never_accept_provenance(self):
        assert "provenance" not in FeedbackRequest.model_fields
        assert "provenance" not in HostFeedbackRequest.model_fields

    @pytest.mark.asyncio
    async def test_success_uses_admin_workspace_gate_and_host_seam(
        self, service, perm, context_id, memory_id
    ):
        target_workspace_id = OPERATOR_USER["current_workspace_id"]
        context = MagicMock(workspace_id=target_workspace_id)
        perm.resolve_context_for_workspace_read = AsyncMock(return_value=context)
        service.record_host_feedback = AsyncMock(
            return_value=MagicMock(id=uuid4(), memory_id=memory_id, helpful=True)
        )
        body = HostFeedbackRequest(
            memory_id=memory_id,
            helpful=True,
            query="bootstrap task 07",
            verdict_source="objective_check",
            verdict_reference="pytest://bootstrap/task-07",
            experiment_id="bootstrap-ab-2026-07-16",
            note="assertions passed",
        )

        response = await record_host_feedback(
            context_id=context_id,
            body=body,
            user=OPERATOR_USER,
            service=service,
            perm=perm,
        )

        assert response.memory_id == memory_id
        perm.resolve_context_for_workspace_read.assert_awaited_once_with(
            "operator",
            context_id,
            required_role="admin",
            key_workspace_id=OPERATOR_USER["api_key_workspace_id"],
        )
        service.record_host_feedback.assert_awaited_once_with(
            context_id=context_id,
            memory_id=memory_id,
            helpful=True,
            user_id="operator",
            actor_email="operator@example.com",
            actor_metadata={"via": "api_key", "key_prefix": "kagura_operator"},
            query="bootstrap task 07",
            verdict_source="objective_check",
            verdict_reference="pytest://bootstrap/task-07",
            experiment_id="bootstrap-ab-2026-07-16",
            note="assertions passed",
        )

    @pytest.mark.asyncio
    async def test_oauth_bearer_operator_is_attributed_as_oauth_not_session(
        self, service, perm, context_id, memory_id
    ):
        # OAuth Bearer principals (auth.dependencies._build_oauth_user_dict)
        # carry oauth_scope and no api_key_workspace_id; the audit `via`
        # marker must not mislabel them as "session" (PR #1307 review).
        oauth_workspace_id = uuid4()
        oauth_user = {
            "user_id": "operator",
            "email": "operator@oauth",
            "role": "user",
            "current_context_id": None,
            "current_workspace_id": oauth_workspace_id,
            "oauth_scope": "memory:admin",
        }
        context = MagicMock(workspace_id=oauth_workspace_id)
        perm.resolve_context_for_workspace_read = AsyncMock(return_value=context)
        service.record_host_feedback = AsyncMock(
            return_value=MagicMock(id=uuid4(), memory_id=memory_id, helpful=True)
        )

        await record_host_feedback(
            context_id=context_id,
            body=HostFeedbackRequest(
                memory_id=memory_id,
                helpful=True,
                verdict_source="objective_check",
                verdict_reference="pytest://oauth/attribution",
            ),
            user=oauth_user,
            service=service,
            perm=perm,
        )

        actor_metadata = service.record_host_feedback.await_args.kwargs["actor_metadata"]
        assert actor_metadata["via"] == "oauth_bearer"

    @pytest.mark.asyncio
    async def test_context_idor_is_uniform_404(self, service, perm, context_id, memory_id):
        perm.resolve_context_for_workspace_read = AsyncMock(
            side_effect=NotFoundException("Context")
        )

        with pytest.raises(NotFoundException):
            await record_host_feedback(
                context_id=context_id,
                body=HostFeedbackRequest(
                    memory_id=memory_id,
                    helpful=False,
                    verdict_source="hitl_approval",
                    verdict_reference="approval://ops/42",
                ),
                user=OPERATOR_USER,
                service=service,
                perm=perm,
            )

        service.record_host_feedback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_workspace_mismatch_is_uniform_404(
        self, service, perm, context_id, memory_id
    ):
        perm.resolve_context_for_workspace_read = AsyncMock(
            return_value=MagicMock(workspace_id=uuid4())
        )

        with pytest.raises(NotFoundException):
            await record_host_feedback(
                context_id=context_id,
                body=HostFeedbackRequest(
                    memory_id=memory_id,
                    helpful=True,
                    verdict_source="trusted_host_check",
                    verdict_reference="host://runner/17",
                ),
                user=OPERATOR_USER,
                service=service,
                perm=perm,
            )

        service.record_host_feedback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_memory_idor_is_uniform_404(self, service, perm, context_id, memory_id):
        perm.resolve_context_for_workspace_read = AsyncMock(
            return_value=MagicMock(workspace_id=OPERATOR_USER["current_workspace_id"])
        )
        service.record_host_feedback = AsyncMock(side_effect=NotFoundException("Memory"))

        with pytest.raises(NotFoundException):
            await record_host_feedback(
                context_id=context_id,
                body=HostFeedbackRequest(
                    memory_id=memory_id,
                    helpful=False,
                    verdict_source="objective_check",
                    verdict_reference="pytest://bootstrap/task-09",
                ),
                user=OPERATOR_USER,
                service=service,
                perm=perm,
            )

    @pytest.mark.asyncio
    async def test_unexpected_host_failure_uses_canonical_internal_error(
        self, service, perm, context_id, memory_id
    ):
        perm.resolve_context_for_workspace_read = AsyncMock(
            return_value=MagicMock(workspace_id=OPERATOR_USER["current_workspace_id"])
        )
        service.record_host_feedback = AsyncMock(side_effect=RuntimeError("db down"))

        with pytest.raises(InternalError, match="Failed to record host feedback"):
            await record_host_feedback(
                context_id=context_id,
                body=HostFeedbackRequest(
                    memory_id=memory_id,
                    helpful=False,
                    verdict_source="objective_check",
                    verdict_reference="pytest://bootstrap/task-09",
                ),
                user=OPERATOR_USER,
                service=service,
                perm=perm,
            )

    @pytest.mark.asyncio
    async def test_agent_bound_credential_is_rejected_even_for_admin(self):
        set_agent_scope(
            AgentScope(
                agent_id=uuid4(),
                enforcement_mode=AGENT_ENFORCEMENT_ENFORCE,
                workspace_id=uuid4(),
            )
        )
        try:
            with pytest.raises(AuthorizationError) as exc:
                await require_host_feedback_operator(OPERATOR_USER)
            assert exc.value.status_code == 403
        finally:
            set_agent_scope(None)

    @pytest.mark.asyncio
    async def test_unbound_operator_credential_is_allowed(self):
        set_agent_scope(None)
        assert await require_host_feedback_operator(OPERATOR_USER) is OPERATOR_USER

    @pytest.mark.parametrize(
        "payload",
        [
            {"verdict_source": "agent_self_report", "verdict_reference": "agent said yes"},
            {"verdict_source": "objective_check", "verdict_reference": "   "},
        ],
    )
    def test_schema_rejects_untrusted_or_blank_verdict(self, payload, memory_id):
        with pytest.raises(ValueError):
            HostFeedbackRequest(memory_id=memory_id, helpful=True, **payload)
