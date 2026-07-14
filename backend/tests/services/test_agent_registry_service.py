"""Unit tests for AgentRegistryService (RFC-0002 P0-1, Issue #1274).

Covers the pure validators, the gate order inside ``create_agent``
(duplicate before quota), governed ``update_agent`` transitions with the
old→new change set, the ``enforce``→``shadow`` widening detector, the
throttled ``touch_last_seen`` write, and the audit-row helper's field
mapping. DB access is mocked — migration/round-trip coverage lives in the
integration gates.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from models.agent import Agent
from services.agent_registry_service import (
    AUDIT_AGENT_ENFORCEMENT_WIDENED,
    AUDIT_AGENT_REGISTERED,
    AgentRegistryService,
    add_agent_audit_row,
    enforcement_widened,
    validate_agent_enforcement_mode,
    validate_agent_name,
    validate_agent_status,
)
from utils.exceptions import ConflictError, QuotaExceededError, ValidationError

WORKSPACE_ID = uuid.uuid4()


def _make_agent(**overrides) -> Agent:
    """Plain ORM instance (no DB) with sane defaults for update tests."""
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
    }
    defaults.update(overrides)
    return Agent(**defaults)


def _service_with_mocks(**method_overrides) -> AgentRegistryService:
    db = MagicMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock()
    db.delete = AsyncMock()
    service = AgentRegistryService(db)
    for name, value in method_overrides.items():
        setattr(service, name, value)
    return service


# ---------------------------------------------------------------------------
# Pure validators
# ---------------------------------------------------------------------------


class TestValidators:
    def test_name_stripped_and_returned(self):
        assert validate_agent_name("  ci-bot  ") == "ci-bot"

    @pytest.mark.parametrize("bad", [None, 42, "", "   ", "x" * 256])
    def test_bad_names_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_agent_name(bad)

    @pytest.mark.parametrize("value", ["active", "suspended", "retired"])
    def test_valid_statuses(self, value):
        assert validate_agent_status(value) == value

    @pytest.mark.parametrize("bad", ["deleted", "ACTIVE", "", None, 1])
    def test_bad_statuses_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_agent_status(bad)

    @pytest.mark.parametrize("value", ["shadow", "enforce"])
    def test_valid_enforcement_modes(self, value):
        assert validate_agent_enforcement_mode(value) == value

    @pytest.mark.parametrize("bad", ["off", "Enforce", "", None])
    def test_bad_enforcement_modes_rejected(self, bad):
        with pytest.raises(ValidationError):
            validate_agent_enforcement_mode(bad)


# ---------------------------------------------------------------------------
# create_agent gate order
# ---------------------------------------------------------------------------


class TestCreateAgent:
    @pytest.mark.asyncio
    async def test_duplicate_name_raises_conflict_before_quota(self):
        service = _service_with_mocks(
            get_agent_by_name=AsyncMock(return_value=_make_agent()),
            count_agents=AsyncMock(),
        )
        with pytest.raises(ConflictError):
            await service.create_agent(
                workspace_id=WORKSPACE_ID, name="ci-bot", owner_user_id="user-1"
            )
        # Gate order: the duplicate check fires before the quota count.
        service.count_agents.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_quota_cap_raises(self):
        from config.settings import get_settings

        service = _service_with_mocks(
            get_agent_by_name=AsyncMock(return_value=None),
            count_agents=AsyncMock(return_value=get_settings().max_agents_per_workspace),
        )
        with pytest.raises(QuotaExceededError):
            await service.create_agent(
                workspace_id=WORKSPACE_ID, name="ci-bot", owner_user_id="user-1"
            )

    @pytest.mark.asyncio
    async def test_create_success_strips_name_and_flushes(self):
        service = _service_with_mocks(
            get_agent_by_name=AsyncMock(return_value=None),
            count_agents=AsyncMock(return_value=0),
        )
        agent = await service.create_agent(
            workspace_id=WORKSPACE_ID,
            name="  ci-bot  ",
            owner_user_id="user-1",
            framework="claude-code",
        )
        assert agent.name == "ci-bot"
        assert agent.framework == "claude-code"
        service.db.add.assert_called_once_with(agent)
        service.db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_overlong_metadata_rejected(self):
        service = _service_with_mocks()
        with pytest.raises(ValidationError):
            await service.create_agent(
                workspace_id=WORKSPACE_ID,
                name="ci-bot",
                owner_user_id="user-1",
                framework="x" * 101,
            )


# ---------------------------------------------------------------------------
# update_agent transitions
# ---------------------------------------------------------------------------


class TestUpdateAgent:
    @pytest.mark.asyncio
    async def test_status_transition_recorded_old_new(self):
        service = _service_with_mocks()
        agent = _make_agent()
        changes = await service.update_agent(agent, {"status": "suspended"})
        assert changes == {"status": {"old": "active", "new": "suspended"}}
        assert agent.status == "suspended"

    @pytest.mark.asyncio
    async def test_noop_assignment_dropped_from_change_set(self):
        service = _service_with_mocks()
        agent = _make_agent()
        changes = await service.update_agent(agent, {"status": "active"})
        assert changes == {}
        service.db.flush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_field_rejected(self):
        service = _service_with_mocks()
        with pytest.raises(ValidationError):
            await service.update_agent(_make_agent(), {"owner_user_id": "attacker"})

    @pytest.mark.asyncio
    async def test_rename_checks_duplicate(self):
        other = _make_agent(name="taken")
        service = _service_with_mocks(get_agent_by_name=AsyncMock(return_value=other))
        with pytest.raises(ConflictError):
            await service.update_agent(_make_agent(), {"name": "taken"})

    @pytest.mark.asyncio
    async def test_metadata_field_cleared_with_none(self):
        service = _service_with_mocks()
        agent = _make_agent(framework="claude-code")
        changes = await service.update_agent(agent, {"framework": None})
        assert changes == {"framework": {"old": "claude-code", "new": None}}
        assert agent.framework is None


class TestEnforcementWidened:
    def test_enforce_to_shadow_is_widening(self):
        assert enforcement_widened({"enforcement_mode": {"old": "enforce", "new": "shadow"}})

    def test_shadow_to_enforce_is_not_widening(self):
        assert not enforcement_widened({"enforcement_mode": {"old": "shadow", "new": "enforce"}})

    def test_unrelated_changes_are_not_widening(self):
        assert not enforcement_widened({"status": {"old": "active", "new": "retired"}})
        assert not enforcement_widened({})


# ---------------------------------------------------------------------------
# touch_last_seen throttle
# ---------------------------------------------------------------------------


class TestTouchLastSeen:
    @pytest.mark.asyncio
    async def test_returns_true_when_row_written(self):
        service = _service_with_mocks()
        service.db.execute = AsyncMock(return_value=SimpleNamespace(rowcount=1))
        assert await service.touch_last_seen(uuid.uuid4()) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_throttled(self):
        # Inside the throttle window the WHERE clause matches no row.
        service = _service_with_mocks()
        service.db.execute = AsyncMock(return_value=SimpleNamespace(rowcount=0))
        assert await service.touch_last_seen(uuid.uuid4()) is False


# ---------------------------------------------------------------------------
# Audit-row helper
# ---------------------------------------------------------------------------


class TestAuditRow:
    def test_row_fields_mirror_security_mutation_lane(self):
        db = MagicMock()
        agent_id = uuid.uuid4()
        add_agent_audit_row(
            db,
            actor_user_id="user-1",
            actor_email=None,
            action=AUDIT_AGENT_REGISTERED,
            agent_id=agent_id,
            workspace_id=WORKSPACE_ID,
            metadata={"via": "mcp", "agent_name": "ci-bot"},
        )
        row = db.add.call_args[0][0]
        assert row.action == AUDIT_AGENT_REGISTERED
        assert row.resource == f"agent:{agent_id}"
        assert row.user_id == "user-1"
        # Email fallback mirrors audit_programmatic_workspace_action.
        assert row.user_email == "user-1@api"
        assert row.user_metadata["workspace_id"] == str(WORKSPACE_ID)
        assert row.user_metadata["via"] == "mcp"

    def test_transition_values_recorded_in_hash_columns(self):
        db = MagicMock()
        add_agent_audit_row(
            db,
            actor_user_id="user-1",
            actor_email="u@example.com",
            action=AUDIT_AGENT_ENFORCEMENT_WIDENED,
            agent_id=uuid.uuid4(),
            workspace_id=WORKSPACE_ID,
            old_value="enforce",
            new_value="shadow",
        )
        row = db.add.call_args[0][0]
        assert row.user_email == "u@example.com"
        # Raw enum values per the roles.py transition precedent.
        assert row.old_value_hash == "enforce"
        assert row.new_value_hash == "shadow"
