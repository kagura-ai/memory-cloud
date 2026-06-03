"""Route-surface tests for the agent session-state REST lane (Issue #906).

Pins the REST wiring around a mocked AgentStateService + mocked PermissionService.
Load-bearing contracts (post-#914 Copilot review):
- every path resolves the context with a UNIFORM 404 first
  (``resolve_context_for_workspace_read``) so a cross-workspace context never
  leaks via a 403 (CWE-639); writes then apply ``check_context_write``;
- a gate denial propagates as the structured MemoryCloudException (404/403),
  not swallowed into a 500;
- null value → ``ValidationError`` (422 with the standard error envelope), not a
  bare HTTPException.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

from api.routes.agent_state import (
    AgentStateSetRequest,
    delete_agent_state,
    get_agent_state,
    list_agent_state,
    set_agent_state,
)
from utils.exceptions import AuthorizationError, NotFoundException, ValidationError

MOCK_USER = {"user_id": "test_user"}


@pytest.fixture
def context_id():
    return uuid4()


@pytest.fixture
def service():
    return MagicMock(
        set_state=AsyncMock(return_value=None),
        get_state=AsyncMock(),
        list_state=AsyncMock(),
        delete_state=AsyncMock(),
    )


@pytest.fixture
def perm():
    return MagicMock(
        resolve_context_for_workspace_read=AsyncMock(),
        check_context_write=AsyncMock(),
    )


class TestSetAgentState:
    @pytest.mark.asyncio
    async def test_set_success_resolves_then_write_gates(self, service, perm, context_id):
        body = AgentStateSetRequest(value={"step": 3}, ttl_seconds=60)
        resp = await set_agent_state(
            context_id=context_id, user=MOCK_USER, body=body, key="run", service=service, perm=perm
        )
        assert resp.key == "run"
        # Uniform-404 reach check happens, AND the editor/owner write gate.
        perm.resolve_context_for_workspace_read.assert_awaited_once_with("test_user", context_id)
        perm.check_context_write.assert_awaited_once_with("test_user", context_id)
        service.set_state.assert_awaited_once_with(context_id, "run", {"step": 3}, ttl_seconds=60)

    @pytest.mark.asyncio
    async def test_set_null_value_raises_validation_error_before_touching_service(
        self, service, perm, context_id
    ):
        with pytest.raises(ValidationError):  # utils.exceptions → 422 + VAL-001 envelope
            await set_agent_state(
                context_id=context_id,
                user=MOCK_USER,
                body=AgentStateSetRequest(value=None),
                key="k",
                service=service,
                perm=perm,
            )
        service.set_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_viewer_denied_propagates_403(self, service, perm, context_id):
        # Reach check passes (workspace member); the write gate 403s a viewer.
        perm.check_context_write.side_effect = AuthorizationError()
        with pytest.raises(AuthorizationError):
            await set_agent_state(
                context_id=context_id,
                user=MOCK_USER,
                body=AgentStateSetRequest(value=1),
                key="k",
                service=service,
                perm=perm,
            )
        service.set_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_unreachable_context_propagates_uniform_404(self, service, perm, context_id):
        # Cross-workspace/unreachable → the RESOLVE step raises a uniform 404
        # before the write gate can 403-leak existence.
        perm.resolve_context_for_workspace_read.side_effect = NotFoundException("Context")
        with pytest.raises(NotFoundException):
            await set_agent_state(
                context_id=context_id,
                user=MOCK_USER,
                body=AgentStateSetRequest(value=1),
                key="k",
                service=service,
                perm=perm,
            )
        perm.check_context_write.assert_not_awaited()
        service.set_state.assert_not_awaited()

    @pytest.mark.parametrize("bad_ttl", [0, -1, -3600])
    def test_non_positive_ttl_rejected_by_schema(self, bad_ttl):
        # gt=0 on the schema is the enforcement point: a 0/negative TTL is a 422
        # at request parsing, before the route runs.
        with pytest.raises(PydanticValidationError):
            AgentStateSetRequest(value=1, ttl_seconds=bad_ttl)


class TestGetAgentState:
    @pytest.mark.asyncio
    async def test_get_present_returns_value_via_uniform_read_gate(self, service, perm, context_id):
        service.get_state.return_value = {"cursor": "abc"}
        resp = await get_agent_state(
            context_id=context_id, user=MOCK_USER, key="run", service=service, perm=perm
        )
        assert resp.key == "run"
        assert resp.value == {"cursor": "abc"}
        perm.resolve_context_for_workspace_read.assert_awaited_once_with("test_user", context_id)
        perm.check_context_write.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_absent_returns_404(self, service, perm, context_id):
        service.get_state.return_value = None
        with pytest.raises(NotFoundException):
            await get_agent_state(
                context_id=context_id, user=MOCK_USER, key="missing", service=service, perm=perm
            )

    @pytest.mark.asyncio
    async def test_get_unreachable_context_propagates_uniform_404(self, service, perm, context_id):
        perm.resolve_context_for_workspace_read.side_effect = NotFoundException("Context")
        with pytest.raises(NotFoundException):
            await get_agent_state(
                context_id=context_id, user=MOCK_USER, key="k", service=service, perm=perm
            )
        service.get_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_falsy_but_present_value_is_returned_not_404(self, service, perm, context_id):
        # A stored ``0`` / ``False`` / ``""`` is a real value — get_state returns
        # None ONLY for absent/expired, so only None maps to 404.
        service.get_state.return_value = 0
        resp = await get_agent_state(
            context_id=context_id, user=MOCK_USER, key="counter", service=service, perm=perm
        )
        assert resp.value == 0


class TestListAgentState:
    @pytest.mark.asyncio
    async def test_list_returns_states_and_count(self, service, perm, context_id):
        service.list_state.return_value = {"a": 1, "b": {"x": 2}}
        resp = await list_agent_state(
            context_id=context_id, user=MOCK_USER, service=service, perm=perm
        )
        assert resp.states == {"a": 1, "b": {"x": 2}}
        assert resp.count == 2
        perm.resolve_context_for_workspace_read.assert_awaited_once_with("test_user", context_id)

    @pytest.mark.asyncio
    async def test_list_empty_returns_200_with_empty_dict(self, service, perm, context_id):
        service.list_state.return_value = {}
        resp = await list_agent_state(
            context_id=context_id, user=MOCK_USER, service=service, perm=perm
        )
        assert resp.states == {}
        assert resp.count == 0

    @pytest.mark.asyncio
    async def test_list_unreachable_context_propagates_uniform_404(self, service, perm, context_id):
        # No-reach caller: the resolve step raises a uniform 404 (not a 403 leak).
        perm.resolve_context_for_workspace_read.side_effect = NotFoundException("Context")
        with pytest.raises(NotFoundException):
            await list_agent_state(
                context_id=context_id, user=MOCK_USER, service=service, perm=perm
            )
        service.list_state.assert_not_awaited()


class TestDeleteAgentState:
    @pytest.mark.asyncio
    async def test_delete_present_resolves_then_write_gates(self, service, perm, context_id):
        service.delete_state.return_value = True
        resp = await delete_agent_state(
            context_id=context_id, user=MOCK_USER, key="run", service=service, perm=perm
        )
        assert resp.key == "run"
        perm.resolve_context_for_workspace_read.assert_awaited_once_with("test_user", context_id)
        perm.check_context_write.assert_awaited_once_with("test_user", context_id)

    @pytest.mark.asyncio
    async def test_delete_absent_returns_404(self, service, perm, context_id):
        service.delete_state.return_value = False
        with pytest.raises(NotFoundException):
            await delete_agent_state(
                context_id=context_id, user=MOCK_USER, key="missing", service=service, perm=perm
            )
