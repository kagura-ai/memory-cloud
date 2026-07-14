"""Agent context-binding service (RFC-0002 P0-2, Issue #1275).

Owns the ``agent_context_bindings`` CRUD (shared by the REST nested routes
and the MCP tools) and the **subtractive** binding evaluation the context
chokepoints call. Design invariants
(``docs/design/agent-registry-and-bindings.md``):

- A binding can only *remove* access the underlying RBAC decision granted —
  the evaluation returns allow/deny; it never grants.
- No binding row for a context = deny under ``enforce`` (default-deny applies
  only to newly bound agents); under ``shadow`` the request proceeds and the
  violation is logged as a would-deny (the durable ``memory_access_events``
  row lands with P0-5, #1278).
- ``contexts.workspace_id == agents.workspace_id`` is validated at binding
  create — a cross-workspace binding row would be inert under the subtractive
  rule, but it is dead weight and a confusing admin surface.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.agent_scope import AgentScope
from models.agent import (
    _ALL_BINDING_WRITE_POLICIES,
    AGENT_ENFORCEMENT_SHADOW,
    Agent,
    AgentContextBinding,
)
from utils.exceptions import ConflictError, NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

# Binding evaluation decisions — vocabulary aligned with the
# memory_access_events.decision CHECK set (F3 design, lands with P0-5).
DECISION_ALLOWED = "allowed"
DECISION_BINDING_DENIED = "binding_denied"
DECISION_WOULD_DENY = "would_deny"

# Access kinds the chokepoints evaluate.
ACCESS_READ = "read"
ACCESS_WRITE = "write"

# Audit action vocabulary (security-mutation lane, extends the P0-1 set).
AUDIT_BINDING_CREATED = "agent_binding_created"
AUDIT_BINDING_UPDATED = "agent_binding_updated"
AUDIT_BINDING_DELETED = "agent_binding_deleted"

_ARRAY_FIELDS = ("allowed_memory_types", "allowed_source_types")


def _validate_type_array(value: Any, field: str) -> list[str] | None:
    """Validate a type/source filter array — RESERVED, enforcement deferred.

    The ``allowed_memory_types`` / ``allowed_source_types`` columns are
    forward-provisioned in the F1 DDL, but per-memory type/source enforcement
    is materially larger than the context-level binding gate (it must filter
    each recall/reference/write by the memory's own type) and lands in a
    follow-up (#1281). To avoid a **fail-open** — an admin setting a restriction
    that is silently ignored, a false sense of containment — CRUD rejects any
    non-NULL value for now. ``NULL`` (= all types, the P0-2-enforced value) is
    the only accepted value. Code-review of #1275. (Same "provisioned-but-
    reserved" posture as ``write_policy='staged'``.)
    """
    if value is None:
        return None
    raise ValidationError(
        f"'{field}' is reserved: per-type binding filters are provisioned but "
        "not yet enforced — set it to null (all types) for now. Type/source "
        "filtering ships in a follow-up (#1281).",
        field=field,
    )


def _validate_write_policy(value: Any) -> str:
    if value not in _ALL_BINDING_WRITE_POLICIES:
        raise ValidationError(
            f"'write_policy' must be one of {list(_ALL_BINDING_WRITE_POLICIES)}",
            field="write_policy",
        )
    return value


async def agent_binding_permits(db: AsyncSession, context_id: uuid.UUID, access: str) -> bool:
    """Reusable subtractive gate for the per-request agent scope.

    Returns True when the request is not an agent credential (scope None) or
    the binding allows / shadow-permits the access; False only on a hard
    ``binding_denied``. This is the single lever the memory-access chokepoints
    (``PermissionService.can_access_memory`` for memory-id-addressed ops,
    ``MemoryService._get_context_isolation_params`` for the declared-context
    write/read path) call so agent-bound REST **and** MCP requests get the
    same intersection the context-resolution chokepoints already apply —
    closing the "MemoryService authorizes via its own RBAC path, not the
    named chokepoint" gap (Copilot/inner review of #1275). No-op cost for
    every non-agent credential (one contextvar read).
    """
    from auth.agent_scope import get_agent_scope

    scope = get_agent_scope()
    if scope is None:
        return True
    allowed, _decision = await AgentBindingService(db).evaluate_context_access(
        scope, context_id, access
    )
    return allowed


class AgentBindingService:
    """CRUD + subtractive evaluation over ``agent_context_bindings``."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # CRUD (owner/admin surfaces)
    # ------------------------------------------------------------------

    async def list_bindings(self, agent: Agent) -> list[AgentContextBinding]:
        result = await self.db.execute(
            select(AgentContextBinding)
            .where(AgentContextBinding.agent_id == agent.id)
            .order_by(AgentContextBinding.created_at.desc(), AgentContextBinding.id)
        )
        return list(result.scalars().all())

    async def get_binding(self, agent: Agent, binding_id: uuid.UUID) -> AgentContextBinding | None:
        result = await self.db.execute(
            select(AgentContextBinding).where(
                AgentContextBinding.id == binding_id,
                AgentContextBinding.agent_id == agent.id,
            )
        )
        return result.scalar_one_or_none()

    async def get_binding_for_context(
        self, agent_id: uuid.UUID, context_id: uuid.UUID
    ) -> AgentContextBinding | None:
        """Fetch the binding row for one (agent, context) pair, or None.

        Used by bootstrap (#1276) to read the ``is_default`` descriptor of an
        explicitly-supplied ``context_id``.
        """
        result = await self.db.execute(
            select(AgentContextBinding).where(
                AgentContextBinding.agent_id == agent_id,
                AgentContextBinding.context_id == context_id,
            )
        )
        return result.scalar_one_or_none()

    async def create_binding(
        self,
        *,
        agent: Agent,
        context_id: uuid.UUID,
        created_by: str,
        can_read: bool = True,
        write_policy: str = "deny",
        is_default: bool = False,
        allowed_memory_types: list[str] | None = None,
        allowed_source_types: list[str] | None = None,
    ) -> AgentContextBinding:
        """Create a binding after validating the workspace boundary.

        Flushed but NOT committed — the caller commits atomically with its
        audit row. The two unique indexes are recognized BY NAME on the flush
        so TOCTOU losers get the same 409 as the pre-checks.

        Raises:
            ValidationError: malformed fields.
            NotFoundException: context missing / cross-workspace (uniform —
                does not confirm foreign contexts exist).
            ConflictError: duplicate (agent, context) binding, or a second
                default binding for the agent.
        """
        from models.auth import Context

        if not isinstance(can_read, bool):
            raise ValidationError("'can_read' must be a boolean", field="can_read")
        if not isinstance(is_default, bool):
            raise ValidationError("'is_default' must be a boolean", field="is_default")
        clean_policy = _validate_write_policy(write_policy)
        clean_memory_types = _validate_type_array(allowed_memory_types, "allowed_memory_types")
        clean_source_types = _validate_type_array(allowed_source_types, "allowed_source_types")

        # Workspace-boundary validation (design-normative). Uniform 404 so a
        # cross-workspace probe cannot confirm a foreign context exists.
        ctx_result = await self.db.execute(
            select(Context).where(Context.id == context_id, Context.deleted_at.is_(None))
        )
        context = ctx_result.scalar_one_or_none()
        if context is None or context.workspace_id != agent.workspace_id:
            raise NotFoundException("Context", str(context_id))

        existing = await self.db.execute(
            select(AgentContextBinding.id).where(
                AgentContextBinding.agent_id == agent.id,
                AgentContextBinding.context_id == context_id,
            )
        )
        if existing.scalar_one_or_none():
            raise ConflictError("A binding for this (agent, context) pair already exists.")

        if is_default:
            default_row = await self.db.execute(
                select(AgentContextBinding.id).where(
                    AgentContextBinding.agent_id == agent.id,
                    AgentContextBinding.is_default.is_(True),
                )
            )
            if default_row.scalar_one_or_none():
                raise ConflictError(
                    "This agent already has a default binding — unset it first "
                    "(is_default is the single source of truth for bootstrap)."
                )

        binding = AgentContextBinding(
            agent_id=agent.id,
            context_id=context_id,
            can_read=can_read,
            write_policy=clean_policy,
            is_default=is_default,
            allowed_memory_types=clean_memory_types,
            allowed_source_types=clean_source_types,
            created_by=created_by,
        )
        self.db.add(binding)
        await self._flush_mapping_conflicts()
        return binding

    async def update_binding(
        self, binding: AgentContextBinding, updates: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """Apply a validated partial update; returns ``{field: {old, new}}``.

        ``context_id`` is immutable (delete + recreate to re-target). No-op
        assignments are dropped so audit rows record only real transitions.
        Flushed but NOT committed.
        """
        changes: dict[str, dict[str, Any]] = {}

        for field, raw in updates.items():
            if field == "can_read" or field == "is_default":
                if not isinstance(raw, bool):
                    raise ValidationError(f"'{field}' must be a boolean", field=field)
                if raw != getattr(binding, field):
                    if field == "is_default" and raw:
                        default_row = await self.db.execute(
                            select(AgentContextBinding.id).where(
                                AgentContextBinding.agent_id == binding.agent_id,
                                AgentContextBinding.is_default.is_(True),
                                AgentContextBinding.id != binding.id,
                            )
                        )
                        if default_row.scalar_one_or_none():
                            raise ConflictError(
                                "This agent already has a default binding — unset it first."
                            )
                    changes[field] = {"old": getattr(binding, field), "new": raw}
                    setattr(binding, field, raw)
            elif field == "write_policy":
                new_policy = _validate_write_policy(raw)
                if new_policy != binding.write_policy:
                    changes[field] = {"old": binding.write_policy, "new": new_policy}
                    binding.write_policy = new_policy
            elif field in _ARRAY_FIELDS:
                new_value = _validate_type_array(raw, field)
                if new_value != getattr(binding, field):
                    changes[field] = {"old": getattr(binding, field), "new": new_value}
                    setattr(binding, field, new_value)
            else:
                raise ValidationError(f"Unknown binding field: '{field}'", field=field)

        if changes:
            await self._flush_mapping_conflicts()
        return changes

    async def delete_binding(self, binding: AgentContextBinding) -> None:
        """Delete a binding row. NOT committed (caller commits with audit)."""
        await self.db.delete(binding)
        await self.db.flush()

    async def _flush_mapping_conflicts(self) -> None:
        """Flush, mapping binding unique-index races to the pre-check 409s."""
        from sqlalchemy.exc import IntegrityError

        from db.constraint_names import (
            AGENT_CTX_BINDING_DEFAULT_UNIQUE,
            AGENT_CTX_BINDING_UNIQUE,
            integrity_error_constraint_name,
        )

        try:
            await self.db.flush()
        except IntegrityError as exc:
            constraint = integrity_error_constraint_name(exc)
            if constraint == AGENT_CTX_BINDING_UNIQUE:
                raise ConflictError(
                    "A binding for this (agent, context) pair already exists."
                ) from exc
            if constraint == AGENT_CTX_BINDING_DEFAULT_UNIQUE:
                raise ConflictError(
                    "This agent already has a default binding — unset it first."
                ) from exc
            raise

    # ------------------------------------------------------------------
    # Subtractive evaluation (chokepoints)
    # ------------------------------------------------------------------

    async def evaluate_context_access(
        self, scope: AgentScope, context_id: uuid.UUID, access: str
    ) -> tuple[bool, str]:
        """Evaluate the binding intersection for one context.

        Returns ``(allowed, decision)`` where decision ∈ {allowed,
        binding_denied, would_deny}. ``would_deny`` means the binding would
        deny but ``enforcement_mode='shadow'`` lets the request proceed under
        legacy semantics (the migration ramp) — callers MUST treat it as
        allowed. This function can only subtract: it is called strictly AFTER
        the existing RBAC decision allowed the request.
        """
        result = await self.db.execute(
            select(AgentContextBinding).where(
                AgentContextBinding.agent_id == scope.agent_id,
                AgentContextBinding.context_id == context_id,
            )
        )
        binding = result.scalar_one_or_none()

        if binding is not None:
            permitted = (
                binding.can_read if access == ACCESS_READ else (binding.write_policy == "direct")
            )
        else:
            # No row = not in the agent's binding set: default-deny (applies
            # only to newly bound agents — unbound keys never reach here).
            permitted = False

        if permitted:
            return True, DECISION_ALLOWED

        if scope.enforcement_mode == AGENT_ENFORCEMENT_SHADOW:
            # Shadow ramp: proceed, but record the violation. The durable
            # would_deny row lands in memory_access_events with P0-5 (#1278).
            logger.warning(
                "agent_binding_would_deny",
                agent_id=str(scope.agent_id),
                context_id=str(context_id),
                access=access,
                bound=binding is not None,
            )
            return True, DECISION_WOULD_DENY

        logger.warning(
            "agent_binding_denied",
            agent_id=str(scope.agent_id),
            context_id=str(context_id),
            access=access,
            bound=binding is not None,
        )
        return False, DECISION_BINDING_DENIED

    async def readable_context_ids(self, agent_id: uuid.UUID) -> set[uuid.UUID]:
        """The agent's read set — for enumeration-surface intersection."""
        result = await self.db.execute(
            select(AgentContextBinding.context_id).where(
                AgentContextBinding.agent_id == agent_id,
                AgentContextBinding.can_read.is_(True),
            )
        )
        return set(result.scalars().all())

    async def resolve_default_binding(
        self, agent_id: uuid.UUID
    ) -> tuple[AgentContextBinding | None, str]:
        """Resolve the agent's default binding for bootstrap (#1276, F2).

        Returns ``(binding, outcome)`` where outcome ∈
        {``"default"``, ``"sole"``, ``"none"``, ``"ambiguous"``}:

        - the row with ``is_default = true`` (``"default"``), else
        - the agent's SOLE binding when exactly one exists (``"sole"``), else
        - ``(None, "none")`` when the agent has no bindings, or
        - ``(None, "ambiguous")`` when it has ≥2 bindings and no default.

        The caller maps ``none``/``ambiguous`` to ``context_id_required``
        WITHOUT enumerating bindings (no existence oracle — F2 normative).
        """
        default_result = await self.db.execute(
            select(AgentContextBinding).where(
                AgentContextBinding.agent_id == agent_id,
                AgentContextBinding.is_default.is_(True),
            )
        )
        default = default_result.scalar_one_or_none()
        if default is not None:
            return default, "default"

        rows = (
            (
                await self.db.execute(
                    select(AgentContextBinding)
                    .where(AgentContextBinding.agent_id == agent_id)
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if len(rows) == 1:
            return rows[0], "sole"
        if len(rows) == 0:
            return None, "none"
        return None, "ambiguous"
