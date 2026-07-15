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

    @pytest.mark.asyncio
    async def test_unknown_mode_fails_closed_intersects(self):
        """code-review: an unrecognized enforcement_mode must fail CLOSED
        (intersect), matching evaluate_context_access which denies on a
        non-shadow mode — the two paths must not disagree."""
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="future_mode"))
        perm = _perm()
        contexts = self._contexts()
        perm._get_accessible_contexts_rbac = AsyncMock(return_value=contexts)
        patcher, instance = _patch_binding_service(True, "allowed")
        instance.readable_context_ids = AsyncMock(return_value={contexts[1].id})
        with patcher:
            result = await perm.get_accessible_contexts("u", WORKSPACE_ID)
        assert result == [contexts[1]]


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


class TestMemoryServiceRecallGate:
    """#1291: MemoryService.recall() applies the subtractive agent-binding gate
    at the SERVICE layer. The MCP recall handler gates via
    ``_resolve_context_for_read``, but the REST ``/memory/recall`` route calls
    ``recall()`` directly — so before this fix an agent-bound key could read a
    binding-denied context via REST. The gate must live in recall() itself."""

    def _svc(self):
        from services.memory_service import MemoryService

        svc = MemoryService.__new__(MemoryService)
        svc.db = MagicMock()
        return svc

    @pytest.mark.asyncio
    async def test_enforce_binding_deny_raises_context_404(self):
        from models.schemas import RecallRequest

        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce"))
        svc = self._svc()
        patcher, _ = _patch_binding_service(False, "binding_denied")
        with patcher, pytest.raises(NotFoundException):
            await svc.recall(
                RecallRequest(query="q"),
                user_id="u",
                current_context_id=uuid.uuid4(),
                current_workspace_id=uuid.uuid4(),
            )

    @pytest.mark.asyncio
    async def test_cross_context_gate_checks_every_entry_not_just_first(self):
        # A denied context in the SECOND slot must still 404 — proves the gate
        # loops over the whole #81 cross-context list, not only the head.
        from models.schemas import RecallRequest

        allowed_id, denied_id = uuid.uuid4(), uuid.uuid4()

        async def _permits(_db, cid, _access, **_audit_kw):  # #1286 passthrough
            return cid != denied_id

        svc = self._svc()
        with (
            patch(
                "services.agent_binding_service.agent_binding_permits",
                new=AsyncMock(side_effect=_permits),
            ),
            pytest.raises(NotFoundException),
        ):
            await svc.recall(
                RecallRequest(query="q"),
                user_id="u",
                current_context_id=allowed_id,
                current_workspace_id=uuid.uuid4(),
                context_ids=[allowed_id, denied_id],
            )

    @pytest.mark.asyncio
    async def test_permitted_is_noop_and_execution_continues(self):
        # When the binding permits (or there is no agent scope), the gate must
        # NOT raise and recall proceeds — proven by reaching the next service
        # call (_resolve_search_mode), which we stub to raise a sentinel.
        from models.schemas import RecallRequest
        from services.memory_service import MemoryService

        svc = self._svc()
        with (
            patch(
                "services.agent_binding_service.agent_binding_permits",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                MemoryService,
                "_resolve_search_mode",
                new=AsyncMock(side_effect=RuntimeError("past-the-gate")),
            ),
            pytest.raises(RuntimeError, match="past-the-gate"),
        ):
            await svc.recall(
                RecallRequest(query="q"),
                user_id="u",
                current_context_id=uuid.uuid4(),
                current_workspace_id=uuid.uuid4(),
            )


class TestCanAccessMemoryDenyCapture:
    """#1286 item 2 (P0-5): rbac-shaped denials at the memory-id chokepoint
    persist policy_decision='rbac_denied' rows for agent traffic. The
    binding-shaped denial is emitted inside the binding evaluation itself
    (pinned in test_agent_binding_service) — here we pin that the audit
    identity (operation / requested memory_id) is THREADED through to it."""

    @pytest.mark.asyncio
    async def test_private_context_rbac_deny_emits_rbac_denied(self):
        ws = uuid.uuid4()
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce", workspace_id=ws))
        perm = _perm()
        ctx, mid = uuid.uuid4(), uuid.uuid4()
        with (
            patch(
                "services.context_service.ContextService.is_context_shared",
                AsyncMock(return_value=False),
            ),
            patch(
                "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
            ) as emit,
        ):
            ok = await perm.can_access_memory(
                user_id="caller",
                memory_user_id="author",
                workspace_id=WORKSPACE_ID,
                context_id=ctx,
                operation="reference",
                memory_id=mid,
            )
        assert ok is False
        emit.assert_awaited_once()
        kw = emit.await_args.kwargs
        assert kw["operation"] == "reference"
        assert kw["outcome"] == "denied"
        assert kw["policy_decision"] == "rbac_denied"
        # Credential scope — never the memory's workspace param.
        assert kw["workspace_id"] == ws
        assert kw["user_id"] == "caller"
        assert kw["extra_metadata"]["requested_context_id"] == str(ctx)
        assert kw["extra_metadata"]["requested_memory_id"] == str(mid)
        assert kw.get("context_id") is None
        assert kw.get("memory_id") is None

    @pytest.mark.asyncio
    async def test_non_member_rbac_deny_emits_rbac_denied(self):
        ws = uuid.uuid4()
        set_agent_scope(AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce", workspace_id=ws))
        perm = _perm()
        perm.is_workspace_member = AsyncMock(return_value=False)
        with (
            patch(
                "services.context_service.ContextService.is_context_shared",
                AsyncMock(return_value=True),
            ),
            patch(
                "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
            ) as emit,
        ):
            ok = await perm.can_access_memory(
                user_id="caller",
                memory_user_id="author",
                workspace_id=WORKSPACE_ID,
                context_id=uuid.uuid4(),
                operation="update",
                memory_id=uuid.uuid4(),
            )
        assert ok is False
        emit.assert_awaited_once()
        assert emit.await_args.kwargs["policy_decision"] == "rbac_denied"

    @pytest.mark.asyncio
    async def test_non_agent_rbac_deny_emits_nothing(self):
        # No agent scope (session / plain member key) → D34: only verified
        # agent traffic is audited.
        perm = _perm()
        with (
            patch(
                "services.context_service.ContextService.is_context_shared",
                AsyncMock(return_value=False),
            ),
            patch(
                "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
            ) as emit,
        ):
            ok = await perm.can_access_memory(
                user_id="caller",
                memory_user_id="author",
                workspace_id=WORKSPACE_ID,
                context_id=uuid.uuid4(),
                operation="reference",
                memory_id=uuid.uuid4(),
            )
        assert ok is False
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_operation_rbac_deny_emits_nothing(self):
        # Un-threaded callers (explore — not in the MAE vocabulary yet) keep
        # the pre-#1286 shape: deny without a row.
        set_agent_scope(
            AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce", workspace_id=uuid.uuid4())
        )
        perm = _perm()
        with (
            patch(
                "services.context_service.ContextService.is_context_shared",
                AsyncMock(return_value=False),
            ),
            patch(
                "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
            ) as emit,
        ):
            ok = await perm.can_access_memory(
                user_id="caller",
                memory_user_id="author",
                workspace_id=WORKSPACE_ID,
                context_id=uuid.uuid4(),
            )
        assert ok is False
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_audit_identity_threaded_to_binding_gate(self):
        set_agent_scope(
            AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce", workspace_id=uuid.uuid4())
        )
        perm = _perm()
        mid = uuid.uuid4()
        patcher, instance = _patch_binding_service(False, "binding_denied")
        with patcher:
            ok = await perm.can_access_memory(
                user_id="caller",
                memory_user_id="caller",
                workspace_id=WORKSPACE_ID,
                context_id=uuid.uuid4(),
                access="write",
                operation="forget",
                memory_id=mid,
            )
        assert ok is False
        kw = instance.evaluate_context_access.await_args.kwargs
        assert kw["operation"] == "forget"
        assert kw["user_id"] == "caller"
        assert kw["requested_memory_id"] == mid


class TestMcpPreGateDenyCapture:
    """#1286 item 2 (P0-5): the MCP write pre-gate threads audit identity
    into the binding evaluation with emit_would_deny=False — the service
    layer re-evaluates and emits the single shadow row; the pre-gate is
    responsible only for the hard deny that stops the request here."""

    @pytest.mark.asyncio
    async def test_resolve_context_threads_operation_with_would_deny_suppressed(self):
        from mcp_server.tools._helpers import _ContextNotFoundError, _resolve_context

        set_agent_scope(
            AgentScope(agent_id=AGENT_ID, enforcement_mode="enforce", workspace_id=uuid.uuid4())
        )
        context = SimpleNamespace(id=uuid.uuid4(), workspace_id=WORKSPACE_ID)
        service = MagicMock(get_context=AsyncMock(return_value=context))
        patcher, instance = _patch_binding_service(False, "binding_denied")
        with (
            patch("services.context_service.ContextService", return_value=service),
            patcher,
            pytest.raises(_ContextNotFoundError),
        ):
            await _resolve_context(MagicMock(), "u", context.id, operation="remember")
        kw = instance.evaluate_context_access.await_args.kwargs
        assert kw["operation"] == "remember"
        assert kw["user_id"] == "u"
        assert kw["emit_would_deny"] is False
