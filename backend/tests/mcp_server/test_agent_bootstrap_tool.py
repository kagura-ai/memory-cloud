"""Handler tests for the get_agent_bootstrap MCP tool (Issue #1276).

Pins the arg/dispatch contract, the total-error → _error_response mapping,
and the success envelope shaping. The composition logic itself is covered by
tests/services/test_agent_bootstrap_service.py.
"""

from __future__ import annotations

import json
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_server.tools.agent_bootstrap import handle_get_agent_bootstrap

WORKSPACE_ID = uuid.uuid4()
AGENT_ID = uuid.uuid4()
CONTEXT_ID = uuid.uuid4()


def _payload(result):
    assert len(result) == 1
    return json.loads(result[0].text)


def _enter(stack, *, principal_error=None, envelope=None):
    async def gen():
        yield AsyncMock()

    stack.enter_context(patch("db.base.get_db", new=gen))
    stack.enter_context(patch("mcp_server.tools.agent_bootstrap._log_tool_usage", new=AsyncMock()))
    stack.enter_context(patch("auth.agent_scope.get_agent_scope", return_value=None))

    from services.agent_bootstrap_service import BootstrapError

    svc = MagicMock()
    if principal_error:
        svc.resolve_principal_and_agent = AsyncMock(side_effect=BootstrapError(*principal_error))
    else:
        svc.resolve_principal_and_agent = AsyncMock(
            return_value=(
                SimpleNamespace(workspace_id=WORKSPACE_ID),
                SimpleNamespace(id=AGENT_ID, name="ci-bot", workspace_id=WORKSPACE_ID),
            )
        )
    svc.resolve_context = AsyncMock(
        return_value=(
            SimpleNamespace(id=CONTEXT_ID, workspace_id=WORKSPACE_ID),
            {"context_id": str(CONTEXT_ID), "is_default": True},
        )
    )
    svc.build_envelope = AsyncMock(
        return_value=envelope
        or {
            "status": "success",
            "degraded": False,
            "agent": {"agent_id": str(AGENT_ID)},
            "components": {},
            "correlation": {},
        }
    )
    stack.enter_context(
        patch("services.agent_bootstrap_service.AgentBootstrapService", return_value=svc)
    )
    return svc


class TestArgValidation:
    @pytest.mark.asyncio
    async def test_missing_agent_id(self):
        result = await handle_get_agent_bootstrap(args={}, user_id="u", workspace_id=WORKSPACE_ID)
        assert _payload(result)["error"] == "missing_fields"

    @pytest.mark.asyncio
    async def test_malformed_agent_id(self):
        result = await handle_get_agent_bootstrap(
            args={"agent_id": "nope"}, user_id="u", workspace_id=WORKSPACE_ID
        )
        assert _payload(result)["error"] == "invalid_arguments"

    @pytest.mark.asyncio
    async def test_bad_include_rejected(self):
        result = await handle_get_agent_bootstrap(
            args={"agent_id": str(AGENT_ID), "include": ["bogus"]},
            user_id="u",
            workspace_id=WORKSPACE_ID,
        )
        assert _payload(result)["error"] == "invalid_arguments"


class TestDispatch:
    @pytest.mark.asyncio
    async def test_agent_not_found_maps_error(self):
        with ExitStack() as stack:
            _enter(stack, principal_error=("agent_not_found", "Agent not found."))
            result = await handle_get_agent_bootstrap(
                args={"agent_id": str(AGENT_ID)}, user_id="u", workspace_id=WORKSPACE_ID
            )
        assert _payload(result)["error"] == "agent_not_found"

    @pytest.mark.asyncio
    async def test_success_returns_envelope(self):
        with ExitStack() as stack:
            _enter(stack)
            result = await handle_get_agent_bootstrap(
                args={"agent_id": str(AGENT_ID)}, user_id="u", workspace_id=WORKSPACE_ID
            )
        body = _payload(result)
        assert body["status"] == "success"
        assert body["agent"]["agent_id"] == str(AGENT_ID)
        assert "degraded" in body

    @pytest.mark.asyncio
    async def test_queryless_call_does_not_meter(self):
        with ExitStack() as stack:
            svc = _enter(stack)
            await handle_get_agent_bootstrap(
                args={"agent_id": str(AGENT_ID)}, user_id="u", workspace_id=WORKSPACE_ID
            )
        # recall_metered must be False for a query-less call.
        assert svc.build_envelope.await_args.kwargs["recall_metered"] is False
