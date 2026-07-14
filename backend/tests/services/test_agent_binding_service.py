"""Unit tests for AgentBindingService (RFC-0002 P0-2, Issue #1275).

Pins the subtractive evaluation matrix (read/write × bound/unbound ×
enforce/shadow), the create/update validations (workspace boundary,
duplicate, single default, array semantics incl. the source-type
vocabulary), and the TOCTOU race mapping. DB access is mocked; the
migration/drift/cascade coverage lives in the integration gates.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from auth.agent_scope import AgentScope
from models.agent import Agent, AgentContextBinding
from services.agent_binding_service import (
    ACCESS_READ,
    ACCESS_WRITE,
    DECISION_ALLOWED,
    DECISION_BINDING_DENIED,
    DECISION_WOULD_DENY,
    AgentBindingService,
    _validate_type_array,
)
from utils.exceptions import ConflictError, NotFoundException, ValidationError

WORKSPACE_ID = uuid.uuid4()


def _agent(**overrides) -> Agent:
    defaults = {
        "id": uuid.uuid4(),
        "workspace_id": WORKSPACE_ID,
        "name": "ci-bot",
        "owner_user_id": "user-1",
        "status": "active",
        "enforcement_mode": "enforce",
    }
    defaults.update(overrides)
    return Agent(**defaults)


def _binding(**overrides) -> AgentContextBinding:
    defaults = {
        "id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "context_id": uuid.uuid4(),
        "can_read": True,
        "write_policy": "deny",
        "is_default": False,
        "allowed_memory_types": None,
        "allowed_source_types": None,
        "created_by": "user-1",
    }
    defaults.update(overrides)
    return AgentContextBinding(**defaults)


def _scope(agent_id=None, mode="enforce") -> AgentScope:
    return AgentScope(agent_id=agent_id or uuid.uuid4(), enforcement_mode=mode)


def _service(execute_results: list | None = None) -> AgentBindingService:
    db = MagicMock()
    db.flush = AsyncMock()
    db.delete = AsyncMock()
    if execute_results is not None:
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=r)) for r in execute_results
            ]
        )
    else:
        db.execute = AsyncMock()
    return AgentBindingService(db)


# ---------------------------------------------------------------------------
# Array validation
# ---------------------------------------------------------------------------


class TestTypeArrayValidation:
    def test_null_means_unrestricted(self):
        assert _validate_type_array(None, "allowed_memory_types") is None

    def test_empty_list_means_deny_all(self):
        assert _validate_type_array([], "allowed_memory_types") == []

    def test_memory_types_are_open_vocabulary(self):
        assert _validate_type_array(["learning", "メモ"], "allowed_memory_types") == [
            "learning",
            "メモ",
        ]

    def test_source_types_checked_against_canonical_set(self):
        with pytest.raises(ValidationError):
            _validate_type_array(["not-a-source-type"], "allowed_source_types")

    def test_known_source_types_pass(self):
        from models.memory import _ALL_SOURCE_TYPES

        assert _validate_type_array(list(_ALL_SOURCE_TYPES[:2]), "allowed_source_types") == list(
            _ALL_SOURCE_TYPES[:2]
        )

    @pytest.mark.parametrize("bad", ["str", 42, [1], [""], [None]])
    def test_malformed_arrays_rejected(self, bad):
        with pytest.raises(ValidationError):
            _validate_type_array(bad, "allowed_memory_types")


# ---------------------------------------------------------------------------
# create_binding validations
# ---------------------------------------------------------------------------


class TestCreateBinding:
    @pytest.mark.asyncio
    async def test_cross_workspace_context_uniform_404(self):
        agent = _agent()
        foreign_context = MagicMock(workspace_id=uuid.uuid4())  # != agent.workspace_id
        service = _service(execute_results=[foreign_context])
        with pytest.raises(NotFoundException):
            await service.create_binding(agent=agent, context_id=uuid.uuid4(), created_by="user-1")

    @pytest.mark.asyncio
    async def test_missing_context_uniform_404(self):
        service = _service(execute_results=[None])
        with pytest.raises(NotFoundException):
            await service.create_binding(
                agent=_agent(), context_id=uuid.uuid4(), created_by="user-1"
            )

    @pytest.mark.asyncio
    async def test_duplicate_pair_conflicts(self):
        agent = _agent()
        same_ws_context = MagicMock(workspace_id=agent.workspace_id)
        service = _service(execute_results=[same_ws_context, uuid.uuid4()])
        with pytest.raises(ConflictError):
            await service.create_binding(agent=agent, context_id=uuid.uuid4(), created_by="user-1")

    @pytest.mark.asyncio
    async def test_second_default_conflicts(self):
        agent = _agent()
        same_ws_context = MagicMock(workspace_id=agent.workspace_id)
        # context lookup → ok, duplicate check → none, default check → existing
        service = _service(execute_results=[same_ws_context, None, uuid.uuid4()])
        with pytest.raises(ConflictError):
            await service.create_binding(
                agent=agent, context_id=uuid.uuid4(), created_by="user-1", is_default=True
            )

    @pytest.mark.asyncio
    async def test_create_success_flushes_row(self):
        agent = _agent()
        same_ws_context = MagicMock(workspace_id=agent.workspace_id)
        service = _service(execute_results=[same_ws_context, None])
        binding = await service.create_binding(
            agent=agent,
            context_id=uuid.uuid4(),
            created_by="user-1",
            write_policy="direct",
        )
        assert binding.write_policy == "direct"
        assert binding.created_by == "user-1"
        service.db.add.assert_called_once_with(binding)
        service.db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bad_write_policy_rejected(self):
        with pytest.raises(ValidationError):
            await _service().create_binding(
                agent=_agent(),
                context_id=uuid.uuid4(),
                created_by="user-1",
                write_policy="staged",  # reserved for P1
            )

    @pytest.mark.asyncio
    async def test_flush_race_maps_to_conflict(self):
        from sqlalchemy.exc import IntegrityError

        agent = _agent()
        same_ws_context = MagicMock(workspace_id=agent.workspace_id)
        service = _service(execute_results=[same_ws_context, None])
        orig = Exception("duplicate key")
        orig.constraint_name = "uq_agent_ctx_binding"  # type: ignore[attr-defined]
        service.db.flush = AsyncMock(side_effect=IntegrityError("INSERT", {}, orig))
        with pytest.raises(ConflictError):
            await service.create_binding(agent=agent, context_id=uuid.uuid4(), created_by="user-1")


# ---------------------------------------------------------------------------
# update_binding
# ---------------------------------------------------------------------------


class TestUpdateBinding:
    @pytest.mark.asyncio
    async def test_transition_recorded_old_new(self):
        service = _service()
        binding = _binding()
        changes = await service.update_binding(binding, {"write_policy": "direct"})
        assert changes == {"write_policy": {"old": "deny", "new": "direct"}}
        assert binding.write_policy == "direct"

    @pytest.mark.asyncio
    async def test_noop_dropped(self):
        service = _service()
        changes = await service.update_binding(_binding(), {"can_read": True})
        assert changes == {}
        service.db.flush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError):
            await _service().update_binding(_binding(), {"context_id": uuid.uuid4()})

    @pytest.mark.asyncio
    async def test_promoting_second_default_conflicts(self):
        service = _service(execute_results=[uuid.uuid4()])  # another default exists
        with pytest.raises(ConflictError):
            await service.update_binding(_binding(is_default=False), {"is_default": True})


# ---------------------------------------------------------------------------
# Subtractive evaluation matrix
# ---------------------------------------------------------------------------


class TestEvaluateContextAccess:
    async def _evaluate(self, binding, scope, access):
        service = _service(execute_results=[binding])
        return await service.evaluate_context_access(scope, uuid.uuid4(), access)

    @pytest.mark.asyncio
    async def test_bound_readable_read_allowed(self):
        allowed, decision = await self._evaluate(
            _binding(can_read=True), _scope(mode="enforce"), ACCESS_READ
        )
        assert (allowed, decision) == (True, DECISION_ALLOWED)

    @pytest.mark.asyncio
    async def test_bound_read_false_enforce_denied(self):
        allowed, decision = await self._evaluate(
            _binding(can_read=False), _scope(mode="enforce"), ACCESS_READ
        )
        assert (allowed, decision) == (False, DECISION_BINDING_DENIED)

    @pytest.mark.asyncio
    async def test_write_requires_direct_policy(self):
        allowed, decision = await self._evaluate(
            _binding(write_policy="deny"), _scope(mode="enforce"), ACCESS_WRITE
        )
        assert (allowed, decision) == (False, DECISION_BINDING_DENIED)

        allowed, decision = await self._evaluate(
            _binding(write_policy="direct"), _scope(mode="enforce"), ACCESS_WRITE
        )
        assert (allowed, decision) == (True, DECISION_ALLOWED)

    @pytest.mark.asyncio
    async def test_unbound_context_enforce_default_deny(self):
        allowed, decision = await self._evaluate(None, _scope(mode="enforce"), ACCESS_READ)
        assert (allowed, decision) == (False, DECISION_BINDING_DENIED)

    @pytest.mark.asyncio
    async def test_unbound_context_shadow_proceeds_as_would_deny(self):
        allowed, decision = await self._evaluate(None, _scope(mode="shadow"), ACCESS_READ)
        assert (allowed, decision) == (True, DECISION_WOULD_DENY)

    @pytest.mark.asyncio
    async def test_bound_write_deny_shadow_proceeds_as_would_deny(self):
        allowed, decision = await self._evaluate(
            _binding(write_policy="deny"), _scope(mode="shadow"), ACCESS_WRITE
        )
        assert (allowed, decision) == (True, DECISION_WOULD_DENY)


class TestReadableContextIds:
    @pytest.mark.asyncio
    async def test_returns_read_set(self):
        ids = [uuid.uuid4(), uuid.uuid4()]
        db = MagicMock()
        scalars = MagicMock(all=MagicMock(return_value=ids))
        db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=scalars)))
        service = AgentBindingService(db)
        assert await service.readable_context_ids(uuid.uuid4()) == set(ids)
