"""Chokepoint tests for the subtractive agent-binding filter (#1275).

Pins the load-bearing contracts of the RFC-0002 P0-2 enforcement wiring:

- **Backward-compat**: with no agent scope (every pre-P0-2 credential) the
  filter is a structural no-op — AgentBindingService is never constructed,
  so the RBAC chokepoints stay byte-for-byte on their old path.
- Binding deny surfaces as the uniform ``context_not_found`` 404 (never a
  403 that would confirm existence) on reads AND writes.
- ``shadow`` mode proceeds (would_deny is allowed).
- ``check_context_access`` derives access from the required ContextRole
  (viewer → read, editor/owner → write).
- Enumeration (``get_accessible_contexts``) intersects with the binding
  read set ONLY in enforce mode.
- The MCP write path (``_resolve_context``) applies the write gate with the
  same uniform deny shape.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.agent_scope import AgentScope, set_agent_scope
from services.permission_service import PermissionService
from utils.exceptions import NotFoundException

WORKSPACE_ID = uuid.uuid4()
AGENT_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _clean_scope():
    set_agent_scope(None)
    yield
    set_agent_scope(None)


def _perm() -> PermissionService:
    return PermissionService(MagicMock())


def _patch_binding_service(allowed: bool, decision: str):
    instance = MagicMock(
        evaluate_context_access=AsyncMock(return_value=(allowed, decision)),
        readable_context_ids=AsyncMock(return_value=set()),
    )
    return (
        patch(
            "services.agent_binding_service.AgentBindingService",
            return_value=instance,
        ),
        instance,
    )


class TestApplyAgentBindingFilter:
    @pytest.mark.asyncio
    async def test_no_scope_is_structural_noop(self):
        """Pre-P0-2 credentials must never construct the binding service."""
        patcher, instance = _patch_binding_service(False, "binding_denied")
        with patcher:
            await _perm()._apply_agent_binding_filter(uuid.uuid4(), access="read", user_id="u")
        instance.evaluate_context_access.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enforce_deny_raises_uniform_404(self):
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce"))
        patcher, _ = _patch_binding_service(False, "binding_denied")
        with patcher, pytest.raises(NotFoundException):
            await _perm()._apply_agent_binding_filter(uuid.uuid4(), access="read", user_id="u")

    @pytest.mark.asyncio
    async def test_shadow_would_deny_passes(self):
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="shadow"))
        patcher, _ = _patch_binding_service(True, "would_deny")
        with patcher:
            await _perm()._apply_agent_binding_filter(uuid.uuid4(), access="write", user_id="u")


class TestCheckContextAccessWrapper:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("required_role", "expected_access"),
        [("viewer", "read"), ("editor", "write"), ("owner", "write")],
    )
    async def test_access_kind_derived_from_required_role(self, required_role, expected_access):
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce"))
        perm = _perm()
        context = SimpleNamespace(id=uuid.uuid4())
        perm._check_context_access_rbac = AsyncMock(return_value=(context, "owner"))
        perm._apply_agent_binding_filter = AsyncMock()

        result, _ = await perm.check_context_access("u", context.id, required_role)

        assert result is context
        kwargs = perm._apply_agent_binding_filter.await_args.kwargs
        assert kwargs["access"] == expected_access

    @pytest.mark.asyncio
    async def test_binding_deny_is_404_even_for_writes(self):
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce"))
        perm = _perm()
        perm._check_context_access_rbac = AsyncMock(
            return_value=(SimpleNamespace(id=uuid.uuid4()), "owner")
        )
        patcher, _ = _patch_binding_service(False, "binding_denied")
        with patcher, pytest.raises(NotFoundException):
            await perm.check_context_access("u", uuid.uuid4(), "editor")


class TestEnumerationIntersection:
    def _contexts(self):
        return [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]

    @pytest.mark.asyncio
    async def test_no_scope_returns_full_membership_view(self):
        perm = _perm()
        contexts = self._contexts()
        perm._get_accessible_contexts_rbac = AsyncMock(return_value=contexts)
        assert await perm.get_accessible_contexts("u", WORKSPACE_ID) == contexts

    @pytest.mark.asyncio
    async def test_enforce_mode_intersects_with_read_set(self):
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce"))
        perm = _perm()
        contexts = self._contexts()
        perm._get_accessible_contexts_rbac = AsyncMock(return_value=contexts)
        patcher, instance = _patch_binding_service(True, "allowed")
        instance.readable_context_ids = AsyncMock(return_value={contexts[0].id})
        with patcher:
            result = await perm.get_accessible_contexts("u", WORKSPACE_ID)
        assert result == [contexts[0]]

    @pytest.mark.asyncio
    async def test_shadow_mode_keeps_full_view(self):
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="shadow"))
        perm = _perm()
        contexts = self._contexts()
        perm._get_accessible_contexts_rbac = AsyncMock(return_value=contexts)
        patcher, instance = _patch_binding_service(True, "allowed")
        with patcher:
            result = await perm.get_accessible_contexts("u", WORKSPACE_ID)
        assert result == contexts
        instance.readable_context_ids.assert_not_awaited()


class TestMcpWritePathGate:
    @pytest.mark.asyncio
    async def test_write_deny_raises_uniform_context_not_found(self):
        from mcp_server.tools._helpers import _ContextNotFoundError, _resolve_context

        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce"))
        context = SimpleNamespace(id=uuid.uuid4(), workspace_id=WORKSPACE_ID)
        service = MagicMock(get_context=AsyncMock(return_value=context))
        patcher, instance = _patch_binding_service(False, "binding_denied")
        with (
            patch("services.context_service.ContextService", return_value=service),
            patcher,
            pytest.raises(_ContextNotFoundError),
        ):
            await _resolve_context(MagicMock(), "u", context.id)
        instance.evaluate_context_access.assert_awaited_once()
        assert instance.evaluate_context_access.await_args.args[2] == "write"

    @pytest.mark.asyncio
    async def test_no_scope_write_path_unchanged(self):
        from mcp_server.tools._helpers import _resolve_context

        context = SimpleNamespace(id=uuid.uuid4(), workspace_id=WORKSPACE_ID)
        service = MagicMock(get_context=AsyncMock(return_value=context))
        patcher, instance = _patch_binding_service(False, "binding_denied")
        with patch("services.context_service.ContextService", return_value=service), patcher:
            result = await _resolve_context(MagicMock(), "u", context.id)
        assert result is context
        instance.evaluate_context_access.assert_not_awaited()


class TestCanAccessMemoryBindingGate:
    """#1275: can_access_memory is the memory-id-addressed chokepoint —
    binding subtracts even for the memory owner, honors the access kind, and
    shadow mode proceeds."""

    @pytest.mark.asyncio
    async def test_no_scope_owner_unchanged(self):
        perm = _perm()
        patcher, instance = _patch_binding_service(False, "binding_denied")
        with patcher:
            ok = await perm.can_access_memory(
                user_id="u", memory_user_id="u", workspace_id=WORKSPACE_ID, context_id=uuid.uuid4()
            )
        assert ok is True
        instance.evaluate_context_access.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_owner_still_subtracted_by_binding(self):
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce"))
        perm = _perm()
        patcher, _ = _patch_binding_service(False, "binding_denied")
        with patcher:
            ok = await perm.can_access_memory(
                user_id="u", memory_user_id="u", workspace_id=WORKSPACE_ID, context_id=uuid.uuid4()
            )
        # Owner passes RBAC but the binding denies — the agent cannot reach
        # even its own memory in a bound-denied context.
        assert ok is False

    @pytest.mark.asyncio
    async def test_write_access_kind_forwarded(self):
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce"))
        perm = _perm()
        patcher, instance = _patch_binding_service(True, "allowed")
        with patcher:
            await perm.can_access_memory(
                user_id="u",
                memory_user_id="u",
                workspace_id=WORKSPACE_ID,
                context_id=uuid.uuid4(),
                access="write",
            )
        assert instance.evaluate_context_access.await_args.args[2] == "write"

    @pytest.mark.asyncio
    async def test_shadow_would_deny_permits(self):
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="shadow"))
        perm = _perm()
        patcher, _ = _patch_binding_service(True, "would_deny")
        with patcher:
            ok = await perm.can_access_memory(
                user_id="u", memory_user_id="u", workspace_id=WORKSPACE_ID, context_id=uuid.uuid4()
            )
        assert ok is True


class TestMemoryServiceDeclaredContextGate:
    """#1275: MemoryService._get_context_isolation_params applies the binding
    gate on the declared-context write/read path (remember / recall / forget)."""

    def _service(self, context):
        from services.memory_service import MemoryService

        svc = MemoryService.__new__(MemoryService)
        svc.db = MagicMock()
        svc.context_service = MagicMock(get_context=AsyncMock(return_value=context))
        return svc

    @pytest.mark.asyncio
    async def test_no_scope_passes(self):
        context = SimpleNamespace(id=uuid.uuid4(), workspace_id=WORKSPACE_ID)
        svc = self._service(context)
        patcher, instance = _patch_binding_service(False, "binding_denied")
        with patcher:
            ctx, ws, cid = await svc._get_context_isolation_params("u", context.id, access="write")
        assert ctx is context
        instance.evaluate_context_access.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_write_binding_deny_raises_context_404(self):
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce"))
        context = SimpleNamespace(id=uuid.uuid4(), workspace_id=WORKSPACE_ID)
        svc = self._service(context)
        patcher, _ = _patch_binding_service(False, "binding_denied")
        with patcher, pytest.raises(NotFoundException):
            await svc._get_context_isolation_params("u", context.id, access="write")

    @pytest.mark.asyncio
    async def test_none_context_short_circuits(self):
        svc = self._service(None)
        result = await svc._get_context_isolation_params("u", None, access="write")
        assert result == (None, None, None)
