"""Route-surface tests for the agent session-state REST lane (Issue #906).

Pins the REST wiring around a mocked ``AgentStateService`` + mocked
``PermissionService`` (the CRUD itself is integration-tested via #889). The
load-bearing contracts here are:
- the read/write gate split mirrors the MCP handler: writes (set/delete) go
  through ``check_context_write``, reads (get/list) through
  ``check_context_access`` at VIEWER;
- gate denials propagate as the structured ``MemoryCloudException`` (404/403),
  not swallowed into a 500;
- absent key → 404 on get and delete; null value → 422 on set;
- envelope shapes (key / value / states+count).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.routes.agent_state import (
    AgentStateSetRequest,
    delete_agent_state,
    get_agent_state,
    list_agent_state,
    set_agent_state,
)
from auth.workspace_roles import ContextRole
from utils.exceptions import AuthorizationError, NotFoundException

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
        check_context_write=AsyncMock(),
        check_context_access=AsyncMock(),
    )


class TestSetAgentState:
    @pytest.mark.asyncio
    async def test_set_success_returns_key_and_uses_write_gate(self, service, perm, context_id):
        body = AgentStateSetRequest(value={"step": 3}, ttl_seconds=60)
        resp = await set_agent_state(
            context_id=context_id, user=MOCK_USER, body=body, key="run", service=service, perm=perm
        )
        assert resp.key == "run"
        perm.check_context_write.assert_awaited_once_with("test_user", context_id)
        perm.check_context_access.assert_not_awaited()  # write path must NOT use the read gate
        service.set_state.assert_awaited_once_with(context_id, "run", {"step": 3}, ttl_seconds=60)

    @pytest.mark.asyncio
    async def test_set_null_value_returns_422_before_touching_service(
        self, service, perm, context_id
    ):
        body = AgentStateSetRequest(value=None)
        with pytest.raises(HTTPException) as exc:
            await set_agent_state(
                context_id=context_id,
                user=MOCK_USER,
                body=body,
                key="k",
                service=service,
                perm=perm,
            )
        assert exc.value.status_code == 422
        service.set_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_viewer_denied_propagates_403(self, service, perm, context_id):
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
    async def test_set_unreachable_context_propagates_404(self, service, perm, context_id):
        perm.check_context_write.side_effect = NotFoundException("Context")
        with pytest.raises(NotFoundException):
            await set_agent_state(
                context_id=context_id,
                user=MOCK_USER,
                body=AgentStateSetRequest(value=1),
                key="k",
                service=service,
                perm=perm,
            )

    @pytest.mark.parametrize("bad_ttl", [0, -1, -3600])
    def test_non_positive_ttl_rejected_by_schema(self, bad_ttl):
        # gt=0 on the schema is the enforcement point: a 0/negative TTL is a 422
        # ValidationError at request parsing, before the route runs.
        with pytest.raises(ValidationError):
            AgentStateSetRequest(value=1, ttl_seconds=bad_ttl)


class TestGetAgentState:
    @pytest.mark.asyncio
    async def test_get_present_returns_value_and_uses_read_gate(self, service, perm, context_id):
        service.get_state.return_value = {"cursor": "abc"}
        resp = await get_agent_state(
            context_id=context_id, user=MOCK_USER, key="run", service=service, perm=perm
        )
        assert resp.key == "run"
        assert resp.value == {"cursor": "abc"}
        perm.check_context_access.assert_awaited_once_with(
            "test_user", context_id, required_role=ContextRole.VIEWER
        )
        perm.check_context_write.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_absent_returns_404(self, service, perm, context_id):
        service.get_state.return_value = None
        with pytest.raises(NotFoundException):
            await get_agent_state(
                context_id=context_id, user=MOCK_USER, key="missing", service=service, perm=perm
            )

    @pytest.mark.asyncio
    async def test_get_unreachable_context_propagates_404(self, service, perm, context_id):
        # Cross-workspace / IDOR: the read gate raises a uniform 404 and the route
        # must propagate it (not swallow into the except-Exception 500 arm).
        perm.check_context_access.side_effect = NotFoundException("Context")
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
        perm.check_context_access.assert_awaited_once_with(
            "test_user", context_id, required_role=ContextRole.VIEWER
        )

    @pytest.mark.asyncio
    async def test_list_empty_returns_200_with_empty_dict(self, service, perm, context_id):
        service.list_state.return_value = {}
        resp = await list_agent_state(
            context_id=context_id, user=MOCK_USER, service=service, perm=perm
        )
        assert resp.states == {}
        assert resp.count == 0

    @pytest.mark.asyncio
    async def test_list_denied_propagates_403(self, service, perm, context_id):
        # No-access caller: the read gate raises 403 and the route propagates it.
        perm.check_context_access.side_effect = AuthorizationError()
        with pytest.raises(AuthorizationError):
            await list_agent_state(
                context_id=context_id, user=MOCK_USER, service=service, perm=perm
            )
        service.list_state.assert_not_awaited()


class TestDeleteAgentState:
    @pytest.mark.asyncio
    async def test_delete_present_returns_key_and_uses_write_gate(self, service, perm, context_id):
        service.delete_state.return_value = True
        resp = await delete_agent_state(
            context_id=context_id, user=MOCK_USER, key="run", service=service, perm=perm
        )
        assert resp.key == "run"
        perm.check_context_write.assert_awaited_once_with("test_user", context_id)
        perm.check_context_access.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_absent_returns_404(self, service, perm, context_id):
        service.delete_state.return_value = False
        with pytest.raises(NotFoundException):
            await delete_agent_state(
                context_id=context_id, user=MOCK_USER, key="missing", service=service, perm=perm
            )
