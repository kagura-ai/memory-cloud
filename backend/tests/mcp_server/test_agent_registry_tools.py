"""Unit tests for the Agent Registry MCP handlers (Issue #1274).

Pins the gate order (workspace → owner/admin role → service) and the
arg/dispatch contract of the five ``*_agent`` handlers without a database —
the service and role gate are mocked. DB-backed behaviour is covered by the
service unit tests and the migration/drift integration gates.
"""

from __future__ import annotations

import json
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_server.tools._helpers import _error_response
from mcp_server.tools.agent_registry import (
    handle_delete_agent,
    handle_get_agent,
    handle_list_agents,
    handle_register_agent,
    handle_update_agent,
)

WORKSPACE_ID = uuid.uuid4()


def _payload(result):
    assert len(result) == 1
    return json.loads(result[0].text)


def _fake_agent(**overrides):
    defaults = {
        "id": uuid.uuid4(),
        "workspace_id": WORKSPACE_ID,
        "name": "ci-bot",
        "description": None,
        "owner_user_id": "user-1",
        "framework": None,
        "environment": None,
        "version": None,
        "status": "active",
        "enforcement_mode": "enforce",
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _enter(stack, *, service, role_error=None):
    """Enter the standard patch set; returns (service, audit recorder)."""

    async def gen():
        yield AsyncMock()

    stack.enter_context(patch("db.base.get_db", new=gen))
    stack.enter_context(
        patch(
            "mcp_server.tools.resource._check_owner_admin_role",
            new=AsyncMock(return_value=role_error),
        )
    )
    stack.enter_context(
        patch(
            "services.agent_registry_service.AgentRegistryService",
            return_value=service,
        )
    )
    audit = MagicMock()
    stack.enter_context(patch("services.agent_registry_service.add_agent_audit_row", new=audit))
    return service, audit


class TestRegisterAgent:
    @pytest.mark.asyncio
    async def test_missing_name_returns_error(self):
        result = await handle_register_agent(args={}, user_id="u", workspace_id=WORKSPACE_ID)
        assert _payload(result)["error"] == "missing_fields"

    @pytest.mark.asyncio
    async def test_no_workspace_returns_error(self):
        result = await handle_register_agent(
            args={"name": "ci-bot"}, user_id="u", workspace_id=None
        )
        assert _payload(result)["error"] == "workspace_required"

    @pytest.mark.asyncio
    async def test_role_gate_short_circuits_before_service(self):
        svc = MagicMock(create_agent=AsyncMock())
        with ExitStack() as stack:
            _enter(
                stack,
                service=svc,
                role_error=_error_response("permission_denied", "owner/admin only"),
            )
            result = await handle_register_agent(
                args={"name": "ci-bot"}, user_id="u", workspace_id=WORKSPACE_ID
            )
        assert _payload(result)["error"] == "permission_denied"
        svc.create_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_audits_and_serializes(self):
        agent = _fake_agent()
        svc = MagicMock(create_agent=AsyncMock(return_value=agent))
        with ExitStack() as stack:
            _, audit = _enter(stack, service=svc)
            result = await handle_register_agent(
                args={"name": "ci-bot", "framework": "claude-code"},
                user_id="user-1",
                workspace_id=WORKSPACE_ID,
            )
        body = _payload(result)
        assert body["status"] == "success"
        assert body["agent"]["name"] == "ci-bot"
        assert body["agent"]["enforcement_mode"] == "enforce"
        audit.assert_called_once()
        assert audit.call_args.kwargs["action"] == "agent_registered"
        assert audit.call_args.kwargs["metadata"]["via"] == "mcp"

    @pytest.mark.asyncio
    async def test_duplicate_maps_to_agent_name_conflict(self):
        from utils.exceptions import ConflictError

        svc = MagicMock(create_agent=AsyncMock(side_effect=ConflictError("Agent exists")))
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_register_agent(
                args={"name": "ci-bot"}, user_id="u", workspace_id=WORKSPACE_ID
            )
        assert _payload(result)["error"] == "agent_name_conflict"


class TestListAndGet:
    @pytest.mark.asyncio
    async def test_list_is_owner_admin_gated(self):
        svc = MagicMock(list_agents=AsyncMock())
        with ExitStack() as stack:
            _enter(
                stack,
                service=svc,
                role_error=_error_response("permission_denied", "owner/admin only"),
            )
            result = await handle_list_agents(args={}, user_id="u", workspace_id=WORKSPACE_ID)
        assert _payload(result)["error"] == "permission_denied"
        svc.list_agents.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_returns_count(self):
        svc = MagicMock(list_agents=AsyncMock(return_value=[_fake_agent(), _fake_agent()]))
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_list_agents(args={}, user_id="u", workspace_id=WORKSPACE_ID)
        body = _payload(result)
        assert body["count"] == 2

    @pytest.mark.asyncio
    async def test_get_malformed_uuid_rejected(self):
        result = await handle_get_agent(
            args={"agent_id": "not-a-uuid"}, user_id="u", workspace_id=WORKSPACE_ID
        )
        assert _payload(result)["error"] == "validation_error"

    @pytest.mark.asyncio
    async def test_get_unknown_agent_uniform_not_found(self):
        svc = MagicMock(get_agent=AsyncMock(return_value=None))
        with ExitStack() as stack:
            _enter(stack, service=svc)
            result = await handle_get_agent(
                args={"agent_id": str(uuid.uuid4())},
                user_id="u",
                workspace_id=WORKSPACE_ID,
            )
        assert _payload(result)["error"] == "agent_not_found"


class TestUpdateAgent:
    @pytest.mark.asyncio
    async def test_no_updatable_fields_rejected(self):
        result = await handle_update_agent(
            args={"agent_id": str(uuid.uuid4())}, user_id="u", workspace_id=WORKSPACE_ID
        )
        assert _payload(result)["error"] == "missing_fields"

    @pytest.mark.asyncio
    async def test_widening_transition_uses_distinct_action(self):
        agent = _fake_agent(enforcement_mode="shadow")
        svc = MagicMock(
            get_agent=AsyncMock(return_value=agent),
            update_agent=AsyncMock(
                return_value={"enforcement_mode": {"old": "enforce", "new": "shadow"}}
            ),
        )
        with ExitStack() as stack:
            _, audit = _enter(stack, service=svc)
            result = await handle_update_agent(
                args={"agent_id": str(agent.id), "enforcement_mode": "shadow"},
                user_id="u",
                workspace_id=WORKSPACE_ID,
            )
        body = _payload(result)
        assert body["status"] == "success"
        assert body["changed"] == ["enforcement_mode"]
        assert audit.call_args.kwargs["action"] == "agent_enforcement_widened"
        assert audit.call_args.kwargs["old_value"] == "enforce"
        assert audit.call_args.kwargs["new_value"] == "shadow"

    @pytest.mark.asyncio
    async def test_combined_status_enforcement_patch_emits_two_rows(self):
        # #1294: a combined status+enforcement PATCH on the MCP surface must not
        # collapse into one audit row — parity with the REST surface.
        agent = _fake_agent()
        svc = MagicMock(
            get_agent=AsyncMock(return_value=agent),
            update_agent=AsyncMock(
                return_value={
                    "status": {"old": "active", "new": "retired"},
                    "enforcement_mode": {"old": "enforce", "new": "shadow"},
                }
            ),
        )
        with ExitStack() as stack:
            _, audit = _enter(stack, service=svc)
            result = await handle_update_agent(
                args={"agent_id": str(agent.id), "status": "retired", "enforcement_mode": "shadow"},
                user_id="u",
                workspace_id=WORKSPACE_ID,
            )
        assert _payload(result)["status"] == "success"
        assert audit.call_count == 2
        emitted = {
            c.kwargs["action"]: (c.kwargs.get("old_value"), c.kwargs.get("new_value"))
            for c in audit.call_args_list
        }
        assert emitted["agent_updated"] == ("active", "retired")
        assert emitted["agent_enforcement_widened"] == ("enforce", "shadow")

    @pytest.mark.asyncio
    async def test_noop_update_writes_no_audit(self):
        agent = _fake_agent()
        svc = MagicMock(
            get_agent=AsyncMock(return_value=agent),
            update_agent=AsyncMock(return_value={}),
        )
        with ExitStack() as stack:
            _, audit = _enter(stack, service=svc)
            result = await handle_update_agent(
                args={"agent_id": str(agent.id), "status": "active"},
                user_id="u",
                workspace_id=WORKSPACE_ID,
            )
        assert _payload(result)["status"] == "success"
        audit.assert_not_called()


class TestDeleteAgent:
    @pytest.mark.asyncio
    async def test_delete_audits_then_deletes(self):
        agent = _fake_agent()
        svc = MagicMock(
            get_agent=AsyncMock(return_value=agent),
            delete_agent=AsyncMock(),
        )
        with ExitStack() as stack:
            _, audit = _enter(stack, service=svc)
            result = await handle_delete_agent(
                args={"agent_id": str(agent.id)},
                user_id="u",
                workspace_id=WORKSPACE_ID,
            )
        body = _payload(result)
        assert body["deleted"] is True
        assert body["agent_id"] == str(agent.id)
        assert audit.call_args.kwargs["action"] == "agent_deleted"
        svc.delete_agent.assert_awaited_once_with(agent)

    @pytest.mark.asyncio
    async def test_unknown_agent_no_audit(self):
        svc = MagicMock(get_agent=AsyncMock(return_value=None), delete_agent=AsyncMock())
        with ExitStack() as stack:
            _, audit = _enter(stack, service=svc)
            result = await handle_delete_agent(
                args={"agent_id": str(uuid.uuid4())},
                user_id="u",
                workspace_id=WORKSPACE_ID,
            )
        assert _payload(result)["error"] == "agent_not_found"
        audit.assert_not_called()
        svc.delete_agent.assert_not_called()
