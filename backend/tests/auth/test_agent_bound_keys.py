"""Unit tests for agent-bound key mint/verify gates (RFC-0002 P0-2, #1275).

Pins the verify-time fail-closed kill switch (suspended/retired/missing
agent → key rejected), the defensive workspace re-assert, the VerifiedKey
agent fields, the mint-time gates in ``create_key``, and the per-request
agent-scope propagation helpers. DB access is mocked.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.agent_scope import (
    AgentScope,
    get_agent_scope,
    set_agent_scope,
    set_agent_scope_from_verified,
)
from auth.api_keys import APIKeyManager, VerifiedKey

WORKSPACE_ID = uuid.uuid4()
AGENT_ID = uuid.uuid4()


def _key_record(**overrides):
    defaults = {
        "id": 7,
        "key_hash": "0" * 64,
        "key_prefix": "kagura_test_pref",
        "user_id": "svc-member",
        "workspace_id": WORKSPACE_ID,
        "bound_context_id": None,
        "agent_id": AGENT_ID,
        "revoked_at": None,
        "expires_at": None,
        "last_used_at": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _agent_row(**overrides):
    defaults = {
        "id": AGENT_ID,
        "workspace_id": WORKSPACE_ID,
        "status": "active",
        "enforcement_mode": "enforce",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _manager(execute_rows: list) -> APIKeyManager:
    """Manager whose db.execute yields the given scalar rows in order."""
    db = MagicMock()
    db.flush = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[MagicMock(scalar_one_or_none=MagicMock(return_value=r)) for r in execute_rows]
    )
    return APIKeyManager(db)


def _patch_hash(key_record):
    """Make _hash_key deterministic so the constant-time compare passes."""
    return patch.object(APIKeyManager, "_hash_key", return_value=key_record.key_hash)


class TestVerifyKeyKillSwitch:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["suspended", "retired"])
    async def test_non_active_agent_rejects_key(self, status):
        key = _key_record()
        manager = _manager([key, _agent_row(status=status)])
        with _patch_hash(key):
            assert await manager.verify_key("kagura_x") is None

    @pytest.mark.asyncio
    async def test_missing_agent_rejects_key(self):
        key = _key_record()
        manager = _manager([key, None])
        with _patch_hash(key):
            assert await manager.verify_key("kagura_x") is None

    @pytest.mark.asyncio
    async def test_workspace_mismatch_rejects_key(self):
        key = _key_record()
        manager = _manager([key, _agent_row(workspace_id=uuid.uuid4())])
        with _patch_hash(key):
            assert await manager.verify_key("kagura_x") is None

    @pytest.mark.asyncio
    async def test_active_agent_returns_agent_fields_and_touches_liveness(self):
        key = _key_record()
        agent = _agent_row(enforcement_mode="shadow")
        manager = _manager([key, agent])
        with (
            _patch_hash(key),
            patch(
                "services.agent_registry_service.AgentRegistryService.touch_last_seen",
                new=AsyncMock(return_value=True),
            ) as touch,
        ):
            verified = await manager.verify_key("kagura_x")
        assert verified is not None
        assert verified.agent_id == AGENT_ID
        assert verified.agent_enforcement_mode == "shadow"
        touch.assert_awaited_once_with(AGENT_ID)

    @pytest.mark.asyncio
    async def test_unbound_key_never_queries_agents(self):
        """Backward-compat: keys without agent_id stay on the pre-#1275 path
        (exactly one SELECT — the key lookup)."""
        key = _key_record(agent_id=None)
        manager = _manager([key])
        with _patch_hash(key):
            verified = await manager.verify_key("kagura_x")
        assert verified is not None
        assert verified.agent_id is None
        assert verified.agent_enforcement_mode is None
        assert manager.db.execute.await_count == 1


class TestCreateKeyMintGates:
    @pytest.mark.asyncio
    async def test_agent_requires_workspace_scope(self):
        manager = _manager([])
        with pytest.raises(ValueError, match="workspace-scoped"):
            await manager.create_key(name="k", user_id="u", workspace_id=None, agent_id=AGENT_ID)

    @pytest.mark.asyncio
    async def test_agent_and_public_binding_mutually_exclusive(self):
        manager = _manager([])
        with pytest.raises(ValueError, match="mutually exclusive"):
            await manager.create_key(
                name="k",
                user_id="u",
                bound_context_id=uuid.uuid4(),
                agent_id=AGENT_ID,
            )

    @pytest.mark.asyncio
    async def test_unknown_agent_rejected(self):
        manager = _manager([None])
        with pytest.raises(ValueError, match="not found"):
            await manager.create_key(
                name="k", user_id="u", workspace_id=WORKSPACE_ID, agent_id=AGENT_ID
            )

    @pytest.mark.asyncio
    async def test_cross_workspace_agent_rejected(self):
        manager = _manager([_agent_row(workspace_id=uuid.uuid4())])
        with pytest.raises(ValueError, match="workspace mismatch"):
            await manager.create_key(
                name="k", user_id="u", workspace_id=WORKSPACE_ID, agent_id=AGENT_ID
            )

    @pytest.mark.asyncio
    async def test_non_active_agent_rejected_at_mint(self):
        manager = _manager([_agent_row(status="retired")])
        with pytest.raises(ValueError, match="active agents"):
            await manager.create_key(
                name="k", user_id="u", workspace_id=WORKSPACE_ID, agent_id=AGENT_ID
            )


class TestAgentScopePropagation:
    def teardown_method(self):
        set_agent_scope(None)

    def test_default_scope_is_none(self):
        set_agent_scope(None)
        assert get_agent_scope() is None

    def test_verified_agent_key_sets_scope(self):
        verified = VerifiedKey(
            id=1,
            user_id="u",
            workspace_id=WORKSPACE_ID,
            bound_context_id=None,
            key_prefix="kagura_test_pref",
            agent_id=AGENT_ID,
            agent_enforcement_mode="enforce",
        )
        set_agent_scope_from_verified(verified)
        scope = get_agent_scope()
        assert scope == AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce")

    def test_unbound_key_clears_scope(self):
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce"))
        verified = VerifiedKey(
            id=1,
            user_id="u",
            workspace_id=WORKSPACE_ID,
            bound_context_id=None,
            key_prefix="kagura_test_pref",
        )
        set_agent_scope_from_verified(verified)
        assert get_agent_scope() is None

    def test_none_clears_scope(self):
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce"))
        set_agent_scope_from_verified(None)
        assert get_agent_scope() is None

    def test_agent_id_without_mode_fails_closed_to_enforce(self):
        """code-review hardening: an agent-bound key (agent_id set) with a
        missing enforcement_mode is an anomaly — it must default to enforce
        (fail-closed, default-deny with no bindings), never clear the scope
        into unrestricted member access."""
        verified = VerifiedKey(
            id=1,
            user_id="u",
            workspace_id=WORKSPACE_ID,
            bound_context_id=None,
            key_prefix="kagura_test_pref",
            agent_id=AGENT_ID,
            agent_enforcement_mode=None,
        )
        set_agent_scope_from_verified(verified)
        scope = get_agent_scope()
        assert scope == AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce")
