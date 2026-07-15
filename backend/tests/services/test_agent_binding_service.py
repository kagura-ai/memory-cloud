"""Unit tests for AgentBindingService (RFC-0002 P0-2, Issue #1275).

Pins the subtractive evaluation matrix (read/write × bound/unbound ×
enforce/shadow), the create/update validations (workspace boundary,
duplicate, single default, array semantics incl. the source-type
vocabulary), and the TOCTOU race mapping. DB access is mocked; the
migration/drift/cascade coverage lives in the integration gates.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.agent_scope import AgentScope
from models.agent import Agent, AgentContextBinding
from services.agent_binding_service import (
    ACCESS_READ,
    ACCESS_WRITE,
    DECISION_ALLOWED,
    DECISION_BINDING_DENIED,
    DECISION_WOULD_DENY,
    ROW_FILTER_KIND,
    AgentBindingService,
    BindingRowFilter,
    _validate_type_array,
    binding_row_filters_for_contexts,
    emit_row_filter_would_deny,
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


def _scope(agent_id=None, mode="enforce", workspace_id=None) -> AgentScope:
    return AgentScope(
        agent_id=agent_id or uuid.uuid4(), enforcement_mode=mode, workspace_id=workspace_id
    )


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
    """#1299: the #1275 reserved-rejection is lifted — CRUD now accepts real
    filter arrays. NULL = unrestricted, [] = deny-all. Memory types are an
    open vocabulary (structural validation only, String(50)); source types
    validate against the full ``_ALL_SOURCE_TYPES`` set (including the
    server-stamped ``connector``)."""

    def test_null_means_unrestricted(self):
        assert _validate_type_array(None, "allowed_memory_types") is None
        assert _validate_type_array(None, "allowed_source_types") is None

    @pytest.mark.parametrize("field", ["allowed_memory_types", "allowed_source_types"])
    def test_empty_list_means_deny_all(self, field):
        assert _validate_type_array([], field) == []

    def test_memory_types_accept_open_vocabulary(self):
        assert _validate_type_array(["note", "time", "bug-fix"], "allowed_memory_types") == [
            "note",
            "time",
            "bug-fix",
        ]

    def test_source_types_accept_full_vocabulary_including_connector(self):
        # 'connector' is server-stamped (clients cannot send it on remember),
        # but stored rows carry it — the filter vocabulary is all six values.
        assert _validate_type_array(
            ["file", "url", "vault", "api", "manual", "connector"], "allowed_source_types"
        ) == ["file", "url", "vault", "api", "manual", "connector"]

    def test_source_types_reject_unknown_value(self):
        with pytest.raises(ValidationError, match="allowed_source_types"):
            _validate_type_array(["bogus"], "allowed_source_types")

    @pytest.mark.parametrize("bad", ["str", 42, {"a": 1}])
    @pytest.mark.parametrize("field", ["allowed_memory_types", "allowed_source_types"])
    def test_non_list_rejected(self, bad, field):
        with pytest.raises(ValidationError):
            _validate_type_array(bad, field)

    @pytest.mark.parametrize("bad_element", [42, None, "", "   "])
    def test_non_string_or_blank_element_rejected(self, bad_element):
        with pytest.raises(ValidationError):
            _validate_type_array([bad_element], "allowed_memory_types")

    def test_memory_type_element_over_column_width_rejected(self):
        # ARRAY(String(50)) — reject at validation instead of a DB-layer 500.
        with pytest.raises(ValidationError):
            _validate_type_array(["x" * 51], "allowed_memory_types")

    def test_duplicate_elements_rejected(self):
        with pytest.raises(ValidationError, match="duplicate"):
            _validate_type_array(["note", "note"], "allowed_memory_types")


class TestBindingRowFilter:
    """#1299: the per-memory row filter derived from a binding's type/source
    arrays. NULL = unrestricted, [] = deny-all; membership otherwise. The
    empty-set case MUST NOT be truthiness-collapsed into 'unrestricted'."""

    def test_no_arrays_means_no_filter(self):
        f = BindingRowFilter.from_binding(_binding())
        assert f is None

    def test_null_arrays_unrestricted_when_other_set(self):
        f = BindingRowFilter.from_binding(_binding(allowed_memory_types=["note"]))
        assert f is not None
        assert f.permits("note", "manual") is True
        assert f.permits("note", "connector") is True  # source unrestricted

    def test_type_membership(self):
        f = BindingRowFilter.from_binding(_binding(allowed_memory_types=["note", "time"]))
        assert f.permits("time", "manual") is True
        assert f.permits("decision", "manual") is False

    def test_source_membership(self):
        f = BindingRowFilter.from_binding(_binding(allowed_source_types=["manual", "api"]))
        assert f.permits("note", "api") is True
        assert f.permits("note", "connector") is False

    def test_empty_array_denies_all_not_falsy(self):
        # [] = deny-all is normative — a truthiness check would fail open.
        f = BindingRowFilter.from_binding(_binding(allowed_memory_types=[]))
        assert f is not None
        assert f.permits("note", "manual") is False
        assert f.permits("anything", "manual") is False

    def test_both_arrays_must_pass(self):
        f = BindingRowFilter.from_binding(
            _binding(allowed_memory_types=["note"], allowed_source_types=["manual"])
        )
        assert f.permits("note", "manual") is True
        assert f.permits("note", "api") is False
        assert f.permits("time", "manual") is False

    def test_missing_row_value_fails_closed_against_restricted_array(self):
        f = BindingRowFilter.from_binding(_binding(allowed_memory_types=["note"]))
        assert f.permits(None, "manual") is False


class TestBindingRowFiltersForContexts:
    """#1299: bulk fetch of row filters for the recall/explore candidate
    contexts — one SELECT, entries only for bindings that actually restrict."""

    @pytest.mark.asyncio
    async def test_returns_filters_only_for_restricting_bindings(self):
        ctx_restricted = uuid.uuid4()
        ctx_unrestricted = uuid.uuid4()
        rows = [
            _binding(context_id=ctx_restricted, allowed_memory_types=["note"]),
            _binding(context_id=ctx_unrestricted),  # both arrays NULL — omitted
        ]
        db = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=rows)
        db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=scalars)))
        scope = _scope()

        filters = await binding_row_filters_for_contexts(
            db, scope, [ctx_restricted, ctx_unrestricted]
        )
        assert set(filters) == {ctx_restricted}
        assert filters[ctx_restricted].permits("note", "manual") is True
        assert filters[ctx_restricted].permits("time", "manual") is False

    @pytest.mark.asyncio
    async def test_empty_context_list_short_circuits(self):
        db = MagicMock()
        db.execute = AsyncMock()
        assert await binding_row_filters_for_contexts(db, _scope(), []) == {}
        db.execute.assert_not_awaited()


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


# ---------------------------------------------------------------------------
# Deny capture (#1286 item 2, P0-5)
# ---------------------------------------------------------------------------


class TestDenyCaptureEmission:
    """The binding-evaluation path persists its deny decisions to
    memory_access_events when the caller threads audit identity (operation +
    user_id). Hard deny → outcome='denied' / policy='binding_denied'; shadow
    → outcome='success' / policy='would_deny'. The requested identifiers ride
    event_metadata as claims — the authoritative context_id / memory_id
    columns stay NULL — and workspace_id is the CREDENTIAL scope
    (AgentScope.workspace_id), never the requested one."""

    async def _evaluate(self, binding, scope, access, ctx=None, **kw):
        service = _service(execute_results=[binding])
        ctx = ctx or uuid.uuid4()
        with patch(
            "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
        ) as emit:
            result = await service.evaluate_context_access(scope, ctx, access, **kw)
        return result, emit, ctx

    @pytest.mark.asyncio
    async def test_enforce_deny_emits_denied_row(self):
        ws = uuid.uuid4()
        (allowed, decision), emit, ctx = await self._evaluate(
            None,
            _scope(mode="enforce", workspace_id=ws),
            ACCESS_READ,
            operation="recall",
            user_id="caller",
        )
        assert (allowed, decision) == (False, DECISION_BINDING_DENIED)
        emit.assert_awaited_once()
        kw = emit.await_args.kwargs
        assert kw["operation"] == "recall"
        assert kw["outcome"] == "denied"
        assert kw["policy_decision"] == DECISION_BINDING_DENIED
        assert kw["workspace_id"] == ws
        assert kw["user_id"] == "caller"
        assert kw["extra_metadata"]["requested_context_id"] == str(ctx)
        assert kw["extra_metadata"]["access"] == ACCESS_READ
        # The requested identifier never lands in the authoritative column.
        assert kw.get("context_id") is None
        assert kw.get("memory_id") is None

    @pytest.mark.asyncio
    async def test_shadow_would_deny_emits_success_row(self):
        ws = uuid.uuid4()
        (allowed, decision), emit, _ = await self._evaluate(
            None,
            _scope(mode="shadow", workspace_id=ws),
            ACCESS_READ,
            operation="recall",
            user_id="caller",
        )
        assert (allowed, decision) == (True, DECISION_WOULD_DENY)
        emit.assert_awaited_once()
        kw = emit.await_args.kwargs
        # The request PROCEEDS in shadow — the outcome is success; the
        # would-deny signal rides policy_decision (the shadow→enforce ramp
        # analysis key).
        assert kw["outcome"] == "success"
        assert kw["policy_decision"] == DECISION_WOULD_DENY
        assert kw["workspace_id"] == ws

    @pytest.mark.asyncio
    async def test_shadow_emits_unconditionally_dedup_is_writer_side(self):
        # Every gate (pre-gates included) emits its would_deny; when several
        # gates evaluate the SAME denied context in one request, the writer's
        # request-scoped dedup collapses the rows to one — no per-call-site
        # suppression flags (pinned in test_memory_access_event_writer).
        (allowed, decision), emit, _ = await self._evaluate(
            None,
            _scope(mode="shadow", workspace_id=uuid.uuid4()),
            ACCESS_WRITE,
            operation="remember",
            user_id="caller",
        )
        assert (allowed, decision) == (True, DECISION_WOULD_DENY)
        emit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_emission_without_operation(self):
        # Callers that have not threaded audit identity (non-memory ops,
        # enumeration surfaces) keep the pre-#1286 behavior: log only.
        (allowed, _), emit, _ = await self._evaluate(
            None, _scope(mode="enforce", workspace_id=uuid.uuid4()), ACCESS_READ
        )
        assert allowed is False
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowed_no_emission(self):
        (allowed, _), emit, _ = await self._evaluate(
            _binding(can_read=True),
            _scope(mode="enforce", workspace_id=uuid.uuid4()),
            ACCESS_READ,
            operation="recall",
            user_id="caller",
        )
        assert allowed is True
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_requested_memory_id_rides_metadata(self):
        mid = uuid.uuid4()
        (_, _), emit, _ = await self._evaluate(
            None,
            _scope(mode="enforce", workspace_id=uuid.uuid4()),
            ACCESS_WRITE,
            operation="forget",
            user_id="caller",
            requested_memory_id=mid,
        )
        kw = emit.await_args.kwargs
        assert kw["extra_metadata"]["requested_memory_id"] == str(mid)
        assert kw.get("memory_id") is None

    @pytest.mark.asyncio
    async def test_agent_binding_permits_threads_audit_kwargs(self):
        from auth.agent_scope import set_agent_scope
        from services.agent_binding_service import agent_binding_permits

        set_agent_scope(_scope(mode="enforce", workspace_id=uuid.uuid4()))
        try:
            with patch(
                "services.agent_binding_service.AgentBindingService.evaluate_context_access",
                AsyncMock(return_value=(False, DECISION_BINDING_DENIED)),
            ) as ev:
                mid = uuid.uuid4()
                ok = await agent_binding_permits(
                    MagicMock(),
                    uuid.uuid4(),
                    ACCESS_READ,
                    operation="reference",
                    user_id="caller",
                    requested_memory_id=mid,
                )
            assert ok is False
            kw = ev.await_args.kwargs
            assert kw["operation"] == "reference"
            assert kw["user_id"] == "caller"
            assert kw["requested_memory_id"] == mid
        finally:
            set_agent_scope(None)


# ---------------------------------------------------------------------------
# Per-memory type/source row filter on evaluate_context_access (#1299)
# ---------------------------------------------------------------------------


class TestEvaluateContextAccessRowFilter:
    """#1299: when the caller threads the memory row's own type/source_type,
    the evaluation applies the binding's per-memory filter AFTER the
    context-level decision. Enforce fail → binding_denied (same uniform deny
    the caller already maps); shadow fail → would_deny, request proceeds.
    Row-filter emissions carry filter_kind='type_source' so audit analysis
    (and the writer dedup key) can tell them from context-level decisions."""

    async def _evaluate(self, binding, scope, **kw):
        service = _service(execute_results=[binding])
        ctx = uuid.uuid4()
        with patch(
            "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
        ) as emit:
            result = await service.evaluate_context_access(scope, ctx, ACCESS_READ, **kw)
        return result, emit

    @pytest.mark.asyncio
    async def test_enforce_type_mismatch_denies(self):
        (allowed, decision), emit = await self._evaluate(
            _binding(can_read=True, allowed_memory_types=["note"]),
            _scope(mode="enforce", workspace_id=uuid.uuid4()),
            operation="reference",
            user_id="caller",
            memory_type="time",
            memory_source_type="manual",
        )
        assert (allowed, decision) == (False, DECISION_BINDING_DENIED)
        kw = emit.await_args.kwargs
        assert kw["outcome"] == "denied"
        assert kw["policy_decision"] == DECISION_BINDING_DENIED
        assert kw["extra_metadata"]["filter_kind"] == ROW_FILTER_KIND

    @pytest.mark.asyncio
    async def test_enforce_match_allows_without_emission(self):
        (allowed, decision), emit = await self._evaluate(
            _binding(can_read=True, allowed_memory_types=["note"], allowed_source_types=["manual"]),
            _scope(mode="enforce", workspace_id=uuid.uuid4()),
            operation="reference",
            user_id="caller",
            memory_type="note",
            memory_source_type="manual",
        )
        assert (allowed, decision) == (True, DECISION_ALLOWED)
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shadow_type_mismatch_would_deny_and_proceeds(self):
        (allowed, decision), emit = await self._evaluate(
            _binding(can_read=True, allowed_source_types=["manual"]),
            _scope(mode="shadow", workspace_id=uuid.uuid4()),
            operation="reference",
            user_id="caller",
            memory_type="note",
            memory_source_type="connector",
        )
        assert (allowed, decision) == (True, DECISION_WOULD_DENY)
        kw = emit.await_args.kwargs
        assert kw["outcome"] == "success"
        assert kw["policy_decision"] == DECISION_WOULD_DENY
        assert kw["extra_metadata"]["filter_kind"] == ROW_FILTER_KIND

    @pytest.mark.asyncio
    async def test_context_level_deny_wins_without_filter_kind(self):
        # can_read=False denies at context level BEFORE the row filter — the
        # emission must NOT claim a row-filter cause.
        (allowed, decision), emit = await self._evaluate(
            _binding(can_read=False, allowed_memory_types=["note"]),
            _scope(mode="enforce", workspace_id=uuid.uuid4()),
            operation="reference",
            user_id="caller",
            memory_type="note",
            memory_source_type="manual",
        )
        assert (allowed, decision) == (False, DECISION_BINDING_DENIED)
        assert "filter_kind" not in emit.await_args.kwargs["extra_metadata"]

    @pytest.mark.asyncio
    async def test_empty_array_denies_every_row(self):
        (allowed, decision), _ = await self._evaluate(
            _binding(can_read=True, allowed_memory_types=[]),
            _scope(mode="enforce", workspace_id=uuid.uuid4()),
            operation="reference",
            user_id="caller",
            memory_type="note",
            memory_source_type="manual",
        )
        assert (allowed, decision) == (False, DECISION_BINDING_DENIED)

    @pytest.mark.asyncio
    async def test_no_row_params_keeps_context_only_semantics(self):
        # Callers that do not thread the row (context resolution, write
        # lane) get the pre-#1299 context-level evaluation, byte-for-byte.
        (allowed, decision), emit = await self._evaluate(
            _binding(can_read=True, allowed_memory_types=[]),
            _scope(mode="enforce", workspace_id=uuid.uuid4()),
            operation="recall",
            user_id="caller",
        )
        assert (allowed, decision) == (True, DECISION_ALLOWED)
        emit.assert_not_awaited()


class TestEmitRowFilterWouldDeny:
    """#1299: the shadow-mode aggregate for row-level filtering — ONE row per
    (operation, context) with counts + a capped id list, never one per row."""

    @pytest.mark.asyncio
    async def test_aggregate_row_shape(self):
        ws = uuid.uuid4()
        ctx = uuid.uuid4()
        ids = [uuid.uuid4() for _ in range(3)]
        with patch(
            "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
        ) as emit:
            await emit_row_filter_would_deny(
                _scope(mode="shadow", workspace_id=ws),
                operation="recall",
                user_id="caller",
                context_id=ctx,
                denied_memory_ids=ids,
            )
        kw = emit.await_args.kwargs
        assert kw["operation"] == "recall"
        assert kw["outcome"] == "success"
        assert kw["policy_decision"] == DECISION_WOULD_DENY
        assert kw["workspace_id"] == ws
        meta = kw["extra_metadata"]
        assert meta["requested_context_id"] == str(ctx)
        assert meta["access"] == ACCESS_READ
        assert meta["filter_kind"] == ROW_FILTER_KIND
        assert meta["would_deny_count"] == 3
        assert meta["memory_ids"] == [str(i) for i in ids]

    @pytest.mark.asyncio
    async def test_id_list_capped_for_metadata_size(self):
        from services.memory_access_event_writer import MAX_METADATA_MEMORY_IDS

        ids = [uuid.uuid4() for _ in range(MAX_METADATA_MEMORY_IDS + 8)]
        with patch(
            "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
        ) as emit:
            await emit_row_filter_would_deny(
                _scope(mode="shadow", workspace_id=uuid.uuid4()),
                operation="recall",
                user_id="caller",
                context_id=uuid.uuid4(),
                denied_memory_ids=ids,
            )
        meta = emit.await_args.kwargs["extra_metadata"]
        assert meta["would_deny_count"] == MAX_METADATA_MEMORY_IDS + 8
        assert len(meta["memory_ids"]) == MAX_METADATA_MEMORY_IDS

    @pytest.mark.asyncio
    async def test_empty_denied_list_no_emission(self):
        with patch(
            "services.memory_access_event_writer.emit_memory_access_event", AsyncMock()
        ) as emit:
            await emit_row_filter_would_deny(
                _scope(mode="shadow", workspace_id=uuid.uuid4()),
                operation="recall",
                user_id="caller",
                context_id=uuid.uuid4(),
                denied_memory_ids=[],
            )
        emit.assert_not_awaited()
