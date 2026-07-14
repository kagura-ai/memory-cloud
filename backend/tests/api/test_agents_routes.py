"""Route-surface tests for the Agent Registry REST lane (Issue #1274).

Pins the REST wiring around a monkeypatched AgentRegistryService + audit
helper. Load-bearing contracts:
- workspace comes from the authenticated principal's active workspace, and
  ``get_agent`` filters by it (uniform 404 inside the boundary — CWE-639);
- every mutation writes exactly one audit row and commits atomically;
- ``enforce`` → ``shadow`` transitions are recorded under the distinct
  ``agent_enforcement_widened`` action;
- a no-op PATCH writes no audit row and does not commit.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import api.routes.agents as agents_routes
from api.routes.agents import (
    AgentCreate,
    AgentUpdate,
    delete_agent,
    get_agent,
    list_agents,
    register_agent,
    update_agent,
)
from utils.exceptions import NotFoundException, ValidationError

WORKSPACE_ID = uuid.uuid4()
MOCK_USER = {
    "user_id": "user-1",
    "email": "u@example.com",
    "current_workspace_id": str(WORKSPACE_ID),
}


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
        "created_at": datetime(2026, 7, 14, 0, 0, 0),
        "updated_at": datetime(2026, 7, 14, 0, 0, 0),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


@pytest.fixture
def db():
    session = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


@pytest.fixture
def service(monkeypatch):
    instance = MagicMock(
        create_agent=AsyncMock(),
        list_agents=AsyncMock(return_value=[]),
        get_agent=AsyncMock(),
        update_agent=AsyncMock(return_value={}),
        delete_agent=AsyncMock(),
    )
    monkeypatch.setattr(agents_routes, "AgentRegistryService", MagicMock(return_value=instance))
    return instance


@pytest.fixture
def audit(monkeypatch):
    recorder = MagicMock()
    monkeypatch.setattr(agents_routes, "add_agent_audit_row", recorder)
    return recorder


class TestRegisterAgent:
    @pytest.mark.asyncio
    async def test_creates_audits_and_commits(self, db, service, audit):
        agent = _fake_agent()
        service.create_agent.return_value = agent

        result = await register_agent(
            data=AgentCreate(name="ci-bot", framework="claude-code"),
            user=MOCK_USER,
            db=db,
        )

        assert result is agent
        create_kwargs = service.create_agent.await_args.kwargs
        assert create_kwargs["workspace_id"] == WORKSPACE_ID
        assert create_kwargs["owner_user_id"] == "user-1"
        audit.assert_called_once()
        assert audit.call_args.kwargs["action"] == "agent_registered"
        assert audit.call_args.kwargs["metadata"]["via"] == "session"
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_api_key_actor_records_prefix(self, db, service, audit):
        service.create_agent.return_value = _fake_agent()
        user = {**MOCK_USER, "api_key_prefix": "km_test_prefix"}

        await register_agent(data=AgentCreate(name="ci-bot"), user=user, db=db)

        metadata = audit.call_args.kwargs["metadata"]
        assert metadata["via"] == "api_key"
        assert metadata["key_prefix"] == "km_test_prefix"


class TestGetAndList:
    @pytest.mark.asyncio
    async def test_get_unknown_agent_is_uniform_404(self, db, service):
        service.get_agent.return_value = None
        with pytest.raises(NotFoundException) as exc:
            await get_agent(agent_id=uuid.uuid4(), user=MOCK_USER, db=db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_scopes_lookup_to_active_workspace(self, db, service):
        agent = _fake_agent()
        service.get_agent.return_value = agent
        result = await get_agent(agent_id=agent.id, user=MOCK_USER, db=db)
        assert result is agent
        service.get_agent.assert_awaited_once_with(WORKSPACE_ID, agent.id)

    @pytest.mark.asyncio
    async def test_list_returns_envelope(self, db, service):
        service.list_agents.return_value = [_fake_agent()]
        result = await list_agents(user=MOCK_USER, db=db)
        assert result.count == 1
        assert result.agents[0].name == "ci-bot"


class TestUpdateAgent:
    @pytest.mark.asyncio
    async def test_transition_audited_with_old_new(self, db, service, audit):
        agent = _fake_agent()
        service.get_agent.return_value = agent
        service.update_agent.return_value = {"status": {"old": "active", "new": "suspended"}}

        await update_agent(
            agent_id=agent.id,
            data=AgentUpdate(status="suspended"),
            user=MOCK_USER,
            db=db,
        )

        kwargs = audit.call_args.kwargs
        assert kwargs["action"] == "agent_updated"
        assert kwargs["old_value"] == "active"
        assert kwargs["new_value"] == "suspended"
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_enforce_to_shadow_uses_widening_action(self, db, service, audit):
        agent = _fake_agent()
        service.get_agent.return_value = agent
        service.update_agent.return_value = {
            "enforcement_mode": {"old": "enforce", "new": "shadow"}
        }

        await update_agent(
            agent_id=agent.id,
            data=AgentUpdate(enforcement_mode="shadow"),
            user=MOCK_USER,
            db=db,
        )

        assert audit.call_args.kwargs["action"] == "agent_enforcement_widened"

    @pytest.mark.asyncio
    async def test_noop_update_skips_audit_and_commit(self, db, service, audit):
        agent = _fake_agent()
        service.get_agent.return_value = agent
        service.update_agent.return_value = {}

        result = await update_agent(
            agent_id=agent.id,
            data=AgentUpdate(status="active"),
            user=MOCK_USER,
            db=db,
        )

        assert result is agent
        audit.assert_not_called()
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_null_name_rejected(self, db, service):
        service.get_agent.return_value = _fake_agent()
        with pytest.raises(ValidationError) as exc:
            await update_agent(
                agent_id=uuid.uuid4(),
                data=AgentUpdate.model_construct(name=None),
                user=MOCK_USER,
                db=db,
            )
        assert exc.value.status_code == 422
        service.update_agent.assert_not_awaited()


class TestDeleteAgent:
    @pytest.mark.asyncio
    async def test_audits_before_delete_and_commits(self, db, service, audit):
        agent = _fake_agent()
        service.get_agent.return_value = agent

        await delete_agent(agent_id=agent.id, user=MOCK_USER, db=db)

        assert audit.call_args.kwargs["action"] == "agent_deleted"
        service.delete_agent.assert_awaited_once_with(agent)
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_agent_404_no_audit(self, db, service, audit):
        service.get_agent.return_value = None
        with pytest.raises(NotFoundException):
            await delete_agent(agent_id=uuid.uuid4(), user=MOCK_USER, db=db)
        audit.assert_not_called()
        db.commit.assert_not_awaited()
