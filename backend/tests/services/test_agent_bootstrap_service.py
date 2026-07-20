"""Unit tests for AgentBootstrapService (RFC-0002 P0-3, Issue #1276).

Pins the F2 identity rule (agent-bound equality → uniform agent_not_found;
non-agent → owner/admin only, on_behalf_of recorded), default-binding
resolution (no enumeration oracle), the per-component fail-soft envelope
(pinned/recall/upcoming/state/policy), the recall skip-without-query and
trusted-only invariants, and the rate_limited degrade. DB access is mocked;
composition byte-compat is covered by the primitives' own tests.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.agent_bootstrap_service import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_SKIPPED,
    AgentBootstrapService,
    BootstrapError,
    BootstrapParams,
    BootstrapPrincipal,
    parse_include,
    parse_recall_evaluation,
    validate_query,
    validate_session_id,
)

WORKSPACE_ID = uuid.uuid4()
AGENT_ID = uuid.uuid4()
CONTEXT_ID = uuid.uuid4()


def _scope(agent_id=AGENT_ID):
    return SimpleNamespace(agent_id=agent_id, enforcement_mode="enforce")


def _agent():
    return SimpleNamespace(id=AGENT_ID, name="ci-bot", workspace_id=WORKSPACE_ID)


def _context():
    return SimpleNamespace(
        id=CONTEXT_ID,
        workspace_id=WORKSPACE_ID,
        name="ctx",
        display_name="Ctx",
        summary="s",
        usage_guide="use me",
        is_private=False,
        is_locked=False,
    )


def _agent_user():
    return {
        "user_id": "u",
        "current_workspace_id": WORKSPACE_ID,
        "api_key_workspace_id": WORKSPACE_ID,
    }


# ---------------------------------------------------------------------------
# Argument validators
# ---------------------------------------------------------------------------


class TestValidators:
    def test_include_default_all(self):
        assert parse_include(None) == ("pinned", "recall", "upcoming", "state", "policy")

    def test_include_preserves_canonical_order_and_dedups(self):
        assert parse_include(["state", "pinned", "state"]) == ("pinned", "state")

    def test_include_unknown_rejected(self):
        with pytest.raises(BootstrapError):
            parse_include(["pinned", "bogus"])

    @pytest.mark.parametrize("bad", ["x" * 129, "has space", 42])
    def test_session_id_rejected(self, bad):
        with pytest.raises(BootstrapError):
            validate_session_id(bad)

    def test_session_id_ok(self):
        assert validate_session_id("run.1-2_3") == "run.1-2_3"

    def test_query_too_long_rejected(self):
        with pytest.raises(BootstrapError):
            validate_query("x" * 1025)

    def test_recall_evaluation_is_strict_and_bounded(self):
        config = parse_recall_evaluation(
            {"seed": 188, "exploration_floor": 0.05, "candidate_pool_k": 100}
        )
        assert config is not None
        assert config.seed == 188
        assert config.exploration_floor == 0.05
        assert config.candidate_pool_k == 100

        for bad in (
            [],
            {"seed": True, "exploration_floor": 0.05, "candidate_pool_k": 100},
            {"seed": 1, "exploration_floor": float("nan"), "candidate_pool_k": 100},
            {"seed": 1, "exploration_floor": 0.05, "candidate_pool_k": 101},
            {"seed": 1, "exploration_floor": 0.05, "candidate_pool_k": 100, "x": 1},
        ):
            with pytest.raises(BootstrapError):
                parse_recall_evaluation(bad)


# ---------------------------------------------------------------------------
# Identity rule
# ---------------------------------------------------------------------------


class TestIdentityRule:
    @pytest.mark.asyncio
    async def test_agent_key_mismatch_uniform_not_found(self):
        svc = AgentBootstrapService(MagicMock())
        with pytest.raises(BootstrapError) as e:
            await svc.resolve_principal_and_agent(
                requested_agent_id=uuid.uuid4(),  # != scope.agent_id
                user=_agent_user(),
                agent_scope=_scope(),
            )
        assert e.value.code == "agent_not_found"

    @pytest.mark.asyncio
    async def test_agent_key_match_resolves_principal(self):
        svc = AgentBootstrapService(MagicMock())
        with patch(
            "services.agent_registry_service.AgentRegistryService.get_agent",
            new=AsyncMock(return_value=_agent()),
        ):
            principal, agent = await svc.resolve_principal_and_agent(
                requested_agent_id=AGENT_ID, user=_agent_user(), agent_scope=_scope()
            )
        assert principal.principal_type == "agent"
        assert principal.on_behalf_of is None
        assert agent.id == AGENT_ID

    @pytest.mark.asyncio
    async def test_nonexistent_agent_not_found(self):
        svc = AgentBootstrapService(MagicMock())
        with patch(
            "services.agent_registry_service.AgentRegistryService.get_agent",
            new=AsyncMock(return_value=None),
        ):
            with pytest.raises(BootstrapError) as e:
                await svc.resolve_principal_and_agent(
                    requested_agent_id=AGENT_ID, user=_agent_user(), agent_scope=_scope()
                )
        assert e.value.code == "agent_not_found"

    @pytest.mark.asyncio
    async def test_non_agent_owner_records_on_behalf_of(self):
        svc = AgentBootstrapService(MagicMock())
        with (
            patch(
                "services.agent_registry_service.AgentRegistryService.get_agent",
                new=AsyncMock(return_value=_agent()),
            ),
            patch(
                "services.permission_service.PermissionService.check_workspace_admin",
                new=AsyncMock(return_value=SimpleNamespace(role="owner")),
            ),
        ):
            principal, _ = await svc.resolve_principal_and_agent(
                requested_agent_id=AGENT_ID, user=_agent_user(), agent_scope=None
            )
        assert principal.principal_type == "owner"
        assert principal.on_behalf_of == "u"
        assert principal.metadata["on_behalf_of"] == "u"

    @pytest.mark.asyncio
    async def test_non_agent_non_admin_uniform_not_found(self):
        from utils.exceptions import AuthorizationError

        svc = AgentBootstrapService(MagicMock())
        with (
            patch(
                "services.agent_registry_service.AgentRegistryService.get_agent",
                new=AsyncMock(return_value=_agent()),
            ),
            patch(
                "services.permission_service.PermissionService.check_workspace_admin",
                new=AsyncMock(side_effect=AuthorizationError()),
            ),
        ):
            with pytest.raises(BootstrapError) as e:
                await svc.resolve_principal_and_agent(
                    requested_agent_id=AGENT_ID, user=_agent_user(), agent_scope=None
                )
        assert e.value.code == "agent_not_found"


# ---------------------------------------------------------------------------
# Default-binding resolution
# ---------------------------------------------------------------------------


class TestContextResolution:
    def _principal(self):
        return BootstrapPrincipal(user_id="u", workspace_id=WORKSPACE_ID, principal_type="agent")

    @pytest.mark.asyncio
    async def test_no_default_binding_context_id_required(self):
        svc = AgentBootstrapService(MagicMock())
        with patch(
            "services.agent_binding_service.AgentBindingService.resolve_default_binding",
            new=AsyncMock(return_value=(None, "none")),
        ):
            with pytest.raises(BootstrapError) as e:
                await svc.resolve_context(
                    agent=_agent(),
                    params=BootstrapParams(agent_id=AGENT_ID),
                    principal=self._principal(),
                )
        assert e.value.code == "context_id_required"
        # No enumeration oracle — message must not list bindings.
        assert "binding" not in e.value.message.lower() or "default" in e.value.message.lower()

    @pytest.mark.asyncio
    async def test_ambiguous_bindings_context_id_required(self):
        svc = AgentBootstrapService(MagicMock())
        with patch(
            "services.agent_binding_service.AgentBindingService.resolve_default_binding",
            new=AsyncMock(return_value=(None, "ambiguous")),
        ):
            with pytest.raises(BootstrapError) as e:
                await svc.resolve_context(
                    agent=_agent(),
                    params=BootstrapParams(agent_id=AGENT_ID),
                    principal=self._principal(),
                )
        assert e.value.code == "context_id_required"

    @pytest.mark.asyncio
    async def test_default_binding_resolves_context(self):
        svc = AgentBootstrapService(MagicMock())
        binding = SimpleNamespace(context_id=CONTEXT_ID, is_default=True)
        with (
            patch(
                "services.agent_binding_service.AgentBindingService.resolve_default_binding",
                new=AsyncMock(return_value=(binding, "default")),
            ),
            patch(
                "services.permission_service.PermissionService.resolve_context_for_workspace_read",
                new=AsyncMock(return_value=_context()),
            ),
        ):
            context, info = await svc.resolve_context(
                agent=_agent(),
                params=BootstrapParams(agent_id=AGENT_ID),
                principal=self._principal(),
            )
        assert context.id == CONTEXT_ID
        assert info == {"context_id": str(CONTEXT_ID), "is_default": True}

    @pytest.mark.asyncio
    async def test_binding_denied_context_maps_to_context_not_found(self):
        from utils.exceptions import NotFoundException

        svc = AgentBootstrapService(MagicMock())
        with (
            patch(
                "services.agent_binding_service.AgentBindingService.get_binding_for_context",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.permission_service.PermissionService.resolve_context_for_workspace_read",
                new=AsyncMock(side_effect=NotFoundException("Context")),
            ),
        ):
            with pytest.raises(BootstrapError) as e:
                await svc.resolve_context(
                    agent=_agent(),
                    params=BootstrapParams(agent_id=AGENT_ID, context_id=CONTEXT_ID),
                    principal=self._principal(),
                )
        assert e.value.code == "context_not_found"

    @staticmethod
    def _resolver_capturing(decision):
        """A resolve_context_for_workspace_read double honoring the #1402
        decision-out contract: append the evaluated decision to the caller's
        ``binding_decision_out`` holder (as the real resolver does for agent-
        bound requests), then return a context. ``decision=None`` models a
        non-agent request that captures nothing."""

        async def _side(*_args, **kwargs):
            holder = kwargs.get("binding_decision_out")
            if holder is not None and decision is not None:
                holder.append(decision)
            return _context()

        return _side

    @pytest.mark.asyncio
    async def test_resolve_context_threads_bootstrap_operation(self):
        """#1402: bootstrap must thread operation='bootstrap' into the read
        resolver so an enforce-mode binding deny at that pre-gate persists a
        binding_denied audit row (previously un-threaded → silent)."""
        svc = AgentBootstrapService(MagicMock())
        resolver = AsyncMock(return_value=_context())
        with (
            patch(
                "services.agent_binding_service.AgentBindingService.get_binding_for_context",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.permission_service.PermissionService.resolve_context_for_workspace_read",
                new=resolver,
            ),
        ):
            await svc.resolve_context(
                agent=_agent(),
                params=BootstrapParams(agent_id=AGENT_ID, context_id=CONTEXT_ID),
                principal=self._principal(),
            )
        assert resolver.await_args.kwargs.get("operation") == "bootstrap"
        # The decision-out holder must be threaded so the success row can carry
        # the real decision (#1402 code-review).
        assert isinstance(resolver.await_args.kwargs.get("binding_decision_out"), list)

    @pytest.mark.asyncio
    async def test_resolve_context_stamps_allowed_decision(self):
        """#1402: on an enforce-mode allow, the principal carries
        policy_decision='allowed' so the bootstrap success MAE row is no longer
        NULL (the surface that rehydrates cognitive state was the one audited op
        whose rows never carried a binding decision)."""
        svc = AgentBootstrapService(MagicMock())
        principal = self._principal()
        with (
            patch(
                "services.agent_binding_service.AgentBindingService.get_binding_for_context",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.permission_service.PermissionService.resolve_context_for_workspace_read",
                new=self._resolver_capturing("allowed"),
            ),
        ):
            await svc.resolve_context(
                agent=_agent(),
                params=BootstrapParams(agent_id=AGENT_ID, context_id=CONTEXT_ID),
                principal=principal,
            )
        assert principal.metadata.get("policy_decision") == "allowed"

    @pytest.mark.asyncio
    async def test_resolve_context_stamps_real_would_deny_decision(self):
        """#1402 (code-review): a shadow would-deny that PROCEEDS must stamp the
        REAL decision on the success row, not a flat 'allowed' — otherwise the
        canonical bootstrap row contradicts the paired would_deny ramp row that
        evaluate_context_access emits on the shadow path."""
        svc = AgentBootstrapService(MagicMock())
        principal = self._principal()
        with (
            patch(
                "services.agent_binding_service.AgentBindingService.get_binding_for_context",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.permission_service.PermissionService.resolve_context_for_workspace_read",
                new=self._resolver_capturing("would_deny"),
            ),
        ):
            await svc.resolve_context(
                agent=_agent(),
                params=BootstrapParams(agent_id=AGENT_ID, context_id=CONTEXT_ID),
                principal=principal,
            )
        assert principal.metadata.get("policy_decision") == "would_deny"

    @pytest.mark.asyncio
    async def test_resolve_context_no_policy_decision_without_binding(self):
        """#1402: a non-agent principal (resolver captures no decision) leaves
        policy_decision NULL — binding evaluation is not applicable."""
        svc = AgentBootstrapService(MagicMock())
        principal = self._principal()
        with (
            patch(
                "services.agent_binding_service.AgentBindingService.get_binding_for_context",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "services.permission_service.PermissionService.resolve_context_for_workspace_read",
                new=self._resolver_capturing(None),
            ),
        ):
            await svc.resolve_context(
                agent=_agent(),
                params=BootstrapParams(agent_id=AGENT_ID, context_id=CONTEXT_ID),
                principal=principal,
            )
        assert principal.metadata.get("policy_decision") is None


class TestAuditOnBehalfOf:
    """#1276 code-review: operator (owner/admin) bootstraps MUST persist an
    on_behalf_of audit row; agent-bound calls must not."""

    @pytest.mark.asyncio
    async def test_operator_bootstrap_writes_audit_row(self):
        db = MagicMock()
        svc = AgentBootstrapService(db)
        principal = BootstrapPrincipal(
            user_id="op",
            workspace_id=WORKSPACE_ID,
            principal_type="owner",
            on_behalf_of="op",
        )
        recorder = MagicMock()
        with patch("services.agent_registry_service.add_agent_audit_row", new=recorder):
            await svc.audit_on_behalf_of(agent=_agent(), principal=principal, session_id="s1")
        recorder.assert_called_once()
        kwargs = recorder.call_args.kwargs
        assert kwargs["action"] == "agent_bootstrap_on_behalf_of"
        assert kwargs["metadata"]["on_behalf_of"] == "op"
        assert kwargs["metadata"]["principal_type"] == "owner"

    @pytest.mark.asyncio
    async def test_agent_bootstrap_writes_no_audit_row(self):
        db = MagicMock()
        svc = AgentBootstrapService(db)
        principal = BootstrapPrincipal(
            user_id="u", workspace_id=WORKSPACE_ID, principal_type="agent"
        )
        recorder = MagicMock()
        with patch("services.agent_registry_service.add_agent_audit_row", new=recorder):
            await svc.audit_on_behalf_of(agent=_agent(), principal=principal, session_id=None)
        recorder.assert_not_called()


# ---------------------------------------------------------------------------
# Envelope composition + fail-soft
# ---------------------------------------------------------------------------


def _savepoint_db() -> MagicMock:
    """A db mock whose begin_nested() is a working async context manager,
    so _component's per-component SAVEPOINT wrapper runs under test."""

    class _SP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    db = MagicMock()
    db.begin_nested = MagicMock(side_effect=lambda: _SP())
    return db


class TestEnvelope:
    def _principal(self):
        return BootstrapPrincipal(user_id="u", workspace_id=WORKSPACE_ID, principal_type="agent")

    async def _build(self, svc, params, recall_metered=False):
        return await svc.build_envelope(
            agent=_agent(),
            context=_context(),
            binding_info={"context_id": str(CONTEXT_ID), "is_default": True},
            params=params,
            principal=self._principal(),
            recall_metered=recall_metered,
        )

    def _patch_components(self, stack, *, pinned=None, recall=None, upcoming=None, state=None):
        # Patch each component's private helper to return a body dict.

        patches = {
            "_pinned": pinned
            if pinned is not None
            else {"memories": [], "total_available": 0, "truncated": False, "cap": 100},
            "_upcoming": upcoming
            if upcoming is not None
            else {"results": [], "from": "now", "until": None},
            "_state": state if state is not None else {"states": {}, "count": 0},
        }
        for name, body in patches.items():
            stack.enter_context(
                patch.object(AgentBootstrapService, name, new=AsyncMock(return_value=body))
            )

    @pytest.mark.asyncio
    async def test_pinned_forwards_cap_override(self):
        # #1281 item 6: pinned_cap must reach load_pinned (was a silent no-op
        # that always passed cap=None regardless of the caller's override).
        svc = AgentBootstrapService(MagicMock())
        load_pinned = AsyncMock(
            return_value=SimpleNamespace(memories=[], total_available=0, truncated=False, cap=7)
        )
        with patch("services.memory_service.MemoryService") as ms_cls:
            ms_cls.return_value.load_pinned = load_pinned
            await svc._pinned(_context(), self._principal(), 7)
        assert load_pinned.await_args.kwargs["cap"] == 7

    @pytest.mark.asyncio
    async def test_pinned_requests_trusted_only(self):
        # #1293: the behaviour-establishing pinned lane must ask load_pinned for
        # the trusted-only set (parity with the recall lane's trust_tier filter).
        svc = AgentBootstrapService(MagicMock())
        load_pinned = AsyncMock(
            return_value=SimpleNamespace(memories=[], total_available=0, truncated=False, cap=100)
        )
        with patch("services.memory_service.MemoryService") as ms_cls:
            ms_cls.return_value.load_pinned = load_pinned
            await svc._pinned(_context(), self._principal(), None)
        assert load_pinned.await_args.kwargs["trusted_only"] is True

    @pytest.mark.asyncio
    async def test_upcoming_requests_trusted_only(self):
        # #1293: the time-memory lane is behaviour-establishing too — it must
        # pass trusted_only through to the shared upcoming query.
        svc = AgentBootstrapService(MagicMock())
        upcoming = AsyncMock(return_value=[])
        with patch("services.time_memory.query_upcoming_time_memories", new=upcoming):
            await svc._upcoming(_context(), BootstrapParams(agent_id=AGENT_ID))
        assert upcoming.await_args.kwargs["trusted_only"] is True

    @pytest.mark.asyncio
    async def test_recall_evaluation_forwards_policy_and_exposes_only_evidence(self):
        memory_id = uuid.uuid4()
        selection_config = parse_recall_evaluation(
            {"seed": 188, "exploration_floor": 0.05, "candidate_pool_k": 100}
        )
        result = SimpleNamespace(
            results=[],
            selection_evidence={
                "selection_probabilities": {str(memory_id): 0.05},
                "selection_policy": {"name": "deterministic_uniform_mixture_v1"},
            },
        )
        svc = AgentBootstrapService(MagicMock())
        recall = AsyncMock(return_value=result)
        with patch("services.memory_service.MemoryService") as ms_cls:
            ms_cls.return_value.recall = recall
            body = await svc._recall(
                _context(),
                BootstrapParams(
                    agent_id=AGENT_ID,
                    query="hi",
                    recall_evaluation=selection_config,
                ),
                self._principal(),
            )

        request = recall.await_args.args[0]
        assert request.filters == {"trust_tier": "trusted"}
        assert recall.await_args.kwargs["selection_config"] == selection_config
        assert body["selection_probabilities"] == {str(memory_id): 0.05}
        assert body["selection_policy"]["name"] == "deterministic_uniform_mixture_v1"
        assert "content" not in repr(body["selection_policy"])

    @pytest.mark.asyncio
    async def test_recall_component_does_not_wrap_transaction_owner_in_savepoint(self):
        db = _savepoint_db()
        svc = AgentBootstrapService(db)
        with patch.object(
            AgentBootstrapService,
            "_recall",
            new=AsyncMock(return_value={"results": [], "k": 5}),
        ):
            component = await svc._recall_component(
                _context(),
                BootstrapParams(agent_id=AGENT_ID, query="hi"),
                self._principal(),
                False,
            )

        assert component == {"status": STATUS_OK, "results": [], "k": 5}
        db.begin_nested.assert_not_called()

    @pytest.mark.asyncio
    async def test_recall_component_rolls_back_before_later_components_on_error(self):
        db = _savepoint_db()
        db.rollback = AsyncMock()
        svc = AgentBootstrapService(db)
        with patch.object(
            AgentBootstrapService,
            "_recall",
            new=AsyncMock(side_effect=RuntimeError("recall failed")),
        ):
            component = await svc._recall_component(
                _context(),
                BootstrapParams(agent_id=AGENT_ID, query="hi"),
                self._principal(),
                False,
            )

        assert component == {"status": STATUS_ERROR, "error": "component_error"}
        db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_state_component_does_not_wrap_ttl_reap_commit_in_savepoint(self):
        import contextlib

        db = _savepoint_db()
        svc = AgentBootstrapService(db)
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    AgentBootstrapService,
                    "_context_and_instructions",
                    new=AsyncMock(return_value=({"id": str(CONTEXT_ID)}, "g")),
                )
            )
            stack.enter_context(
                patch.object(
                    AgentBootstrapService,
                    "_state",
                    new=AsyncMock(return_value={"states": {}, "count": 0}),
                )
            )
            env = await self._build(
                svc,
                BootstrapParams(agent_id=AGENT_ID, include=("state",)),
            )

        assert env["components"]["state"] == {"status": STATUS_OK, "states": {}, "count": 0}
        db.begin_nested.assert_not_called()

    @pytest.mark.asyncio
    async def test_query_absent_recall_skipped(self):
        import contextlib

        svc = AgentBootstrapService(_savepoint_db())
        with contextlib.ExitStack() as stack:
            self._patch_components(stack)
            stack.enter_context(
                patch.object(
                    AgentBootstrapService,
                    "_context_and_instructions",
                    new=AsyncMock(return_value=({"id": str(CONTEXT_ID)}, "guide\n\ninstr")),
                )
            )
            env = await self._build(svc, BootstrapParams(agent_id=AGENT_ID, query=None))
        assert env["components"]["recall"]["status"] == STATUS_SKIPPED
        assert env["components"]["recall"]["reason"] == "no_query"
        assert env["degraded"] is False
        assert env["instructions"] == "guide\n\ninstr"

    @pytest.mark.asyncio
    async def test_query_present_but_rate_limited_degrades_recall_only(self):
        import contextlib

        svc = AgentBootstrapService(_savepoint_db())
        with contextlib.ExitStack() as stack:
            self._patch_components(stack)
            stack.enter_context(
                patch.object(
                    AgentBootstrapService,
                    "_context_and_instructions",
                    new=AsyncMock(return_value=({"id": str(CONTEXT_ID)}, "g")),
                )
            )
            env = await self._build(
                svc, BootstrapParams(agent_id=AGENT_ID, query="hi"), recall_metered=True
            )
        assert env["components"]["recall"] == {"status": STATUS_ERROR, "error": "rate_limited"}
        # Cheap components still return.
        assert env["components"]["pinned"]["status"] == STATUS_OK
        assert env["components"]["state"]["status"] == STATUS_OK

    @pytest.mark.asyncio
    async def test_component_error_sets_degraded_but_others_return(self):
        import contextlib

        svc = AgentBootstrapService(_savepoint_db())
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    AgentBootstrapService,
                    "_pinned",
                    new=AsyncMock(side_effect=RuntimeError("qdrant down")),
                )
            )
            stack.enter_context(
                patch.object(
                    AgentBootstrapService,
                    "_upcoming",
                    new=AsyncMock(return_value={"results": [], "from": "now", "until": None}),
                )
            )
            stack.enter_context(
                patch.object(
                    AgentBootstrapService,
                    "_state",
                    new=AsyncMock(return_value={"states": {}, "count": 0}),
                )
            )
            stack.enter_context(
                patch.object(
                    AgentBootstrapService,
                    "_context_and_instructions",
                    new=AsyncMock(return_value=({"id": str(CONTEXT_ID)}, "g")),
                )
            )
            env = await self._build(
                svc,
                BootstrapParams(
                    agent_id=AGENT_ID, query=None, include=("pinned", "upcoming", "state")
                ),
            )
        assert env["components"]["pinned"] == {"status": STATUS_ERROR, "error": "component_error"}
        assert env["components"]["upcoming"]["status"] == STATUS_OK
        assert env["degraded"] is True

    @pytest.mark.asyncio
    async def test_include_selector_limits_components(self):
        import contextlib

        svc = AgentBootstrapService(_savepoint_db())
        with contextlib.ExitStack() as stack:
            self._patch_components(stack)
            stack.enter_context(
                patch.object(
                    AgentBootstrapService,
                    "_context_and_instructions",
                    new=AsyncMock(return_value=({"id": str(CONTEXT_ID)}, "g")),
                )
            )
            env = await self._build(svc, BootstrapParams(agent_id=AGENT_ID, include=("state",)))
        assert set(env["components"]) == {"state"}
        assert env["correlation"]["agent_id"] == str(AGENT_ID)


class TestTransactionOwningComponentFailSoft:
    """A raising rollback() inside the except handler must not escape — the
    component stays fail-soft and the bootstrap request survives (PR #1308
    review)."""

    @pytest.mark.asyncio
    async def test_rollback_failure_still_returns_component_error(self):
        db = MagicMock()
        db.rollback = AsyncMock(side_effect=RuntimeError("connection is closed"))
        svc = AgentBootstrapService(db)

        async def boom():
            raise RuntimeError("component blew up")

        result = await svc._transaction_owning_component("recall", boom)
        assert result == {"status": STATUS_ERROR, "error": "component_error"}
        db.rollback.assert_awaited_once()
