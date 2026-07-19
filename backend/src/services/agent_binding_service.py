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
from dataclasses import dataclass
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
from models.memory import _ALL_SOURCE_TYPES
from utils.exceptions import ConflictError, NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

# Binding evaluation decisions — vocabulary aligned with the
# memory_access_events.decision CHECK set (F3 design, lands with P0-5).
DECISION_ALLOWED = "allowed"
DECISION_BINDING_DENIED = "binding_denied"
DECISION_WOULD_DENY = "would_deny"
# #1286 (P0-5): an RBAC-shaped deny observed on the binding-evaluation path
# (can_access_memory collapses RBAC and binding into one bool — the rbac
# branches stamp this so the two deny causes stay distinguishable in audit).
DECISION_RBAC_DENIED = "rbac_denied"

# Access kinds the chokepoints evaluate.
ACCESS_READ = "read"
ACCESS_WRITE = "write"

# Audit action vocabulary (security-mutation lane, extends the P0-1 set).
AUDIT_BINDING_CREATED = "agent_binding_created"
AUDIT_BINDING_UPDATED = "agent_binding_updated"
AUDIT_BINDING_DELETED = "agent_binding_deleted"

_ARRAY_FIELDS = ("allowed_memory_types", "allowed_source_types")

# Column width of allowed_memory_types elements (ARRAY(String(50))) — memory
# types are an open vocabulary (no CHECK anywhere), so validation is purely
# structural; rejecting over-width here keeps a malformed array a 422 instead
# of a DB-layer 500.
_MEMORY_TYPE_MAX_CHARS = 50

# event_metadata claim distinguishing a per-memory row-filter decision (#1299)
# from a context-level binding decision. Participates in the writer's
# would_deny dedup key so the two shadow signals for the same (operation,
# context, access) both persist.
ROW_FILTER_KIND = "type_source"


def _validate_type_array(value: Any, field: str) -> list[str] | None:
    """Validate a type/source filter array (#1299 lifts the #1275 rejection).

    ``NULL`` = unrestricted, ``[]`` = deny-all (F1 semantics, fixed at the
    DDL). Memory types are an open vocabulary — structural validation only
    (non-blank strings within the column width). Source types validate
    against the full ``_ALL_SOURCE_TYPES`` vocabulary *including* the
    server-stamped ``connector`` (clients cannot send it on remember, but
    stored rows carry it, so a filter must be able to name it).
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValidationError(
            f"'{field}' must be null (unrestricted), [] (deny-all), or a list of strings",
            field=field,
        )
    cleaned: list[str] = []
    for item in value:
        # ``bool`` is an ``int``, not a ``str`` — the isinstance guard already
        # rejects it; there is no str subclass we need to special-case.
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(
                f"'{field}' elements must be non-blank strings",
                field=field,
            )
        # Stored types are matched byte-for-byte against each memory's own
        # ``type`` / ``source_type`` (no normalization on the compare side), so
        # reject surrounding whitespace and control/NUL characters here rather
        # than silently storing a value that can never match a real row.
        if item != item.strip():
            raise ValidationError(
                f"'{field}' elements must not have leading/trailing whitespace",
                field=field,
            )
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in item):
            raise ValidationError(
                f"'{field}' elements must not contain control characters",
                field=field,
            )
        if field == "allowed_source_types" and item not in _ALL_SOURCE_TYPES:
            raise ValidationError(
                f"'allowed_source_types' elements must be one of {list(_ALL_SOURCE_TYPES)}",
                field=field,
            )
        if field == "allowed_memory_types" and len(item) > _MEMORY_TYPE_MAX_CHARS:
            raise ValidationError(
                f"'allowed_memory_types' elements must be at most "
                f"{_MEMORY_TYPE_MAX_CHARS} characters",
                field=field,
            )
        cleaned.append(item)
    if len(set(cleaned)) != len(cleaned):
        raise ValidationError(f"'{field}' must not contain duplicate values", field=field)
    return cleaned


@dataclass(frozen=True)
class BindingRowFilter:
    """Per-memory type/source filter derived from one binding row (#1299).

    ``None`` = that dimension is unrestricted; a ``frozenset`` (possibly
    empty) = membership required. The empty set denies every row — the
    normative ``[]`` = deny-all — which is why every check here is an
    ``is not None`` test, never truthiness (an empty frozenset is falsy and
    a truthiness check would silently fail OPEN).
    """

    allowed_memory_types: frozenset[str] | None
    allowed_source_types: frozenset[str] | None

    @classmethod
    def from_binding(cls, binding: AgentContextBinding) -> BindingRowFilter | None:
        """Build the filter, or None when the binding does not restrict."""
        if binding.allowed_memory_types is None and binding.allowed_source_types is None:
            return None
        return cls(
            allowed_memory_types=(
                None
                if binding.allowed_memory_types is None
                else frozenset(binding.allowed_memory_types)
            ),
            allowed_source_types=(
                None
                if binding.allowed_source_types is None
                else frozenset(binding.allowed_source_types)
            ),
        )

    def permits(self, memory_type: str | None, source_type: str | None) -> bool:
        """True when the row passes both dimensions (missing value = deny)."""
        if self.allowed_memory_types is not None and memory_type not in self.allowed_memory_types:
            return False
        return not (
            self.allowed_source_types is not None and source_type not in self.allowed_source_types
        )


async def binding_row_filters_for_contexts(
    db: AsyncSession,
    scope: AgentScope,
    context_ids: list[uuid.UUID],
) -> dict[uuid.UUID, BindingRowFilter]:
    """Bulk-fetch the agent's row filters for a candidate set of contexts.

    One SELECT for the whole recall/explore candidate pool (never per-row —
    the N+1 guard). Entries exist only for bindings that actually restrict
    (at least one non-NULL array); contexts without an entry need no per-row
    evaluation. Context-level access is NOT decided here — the context gates
    ran upstream; this is purely the row-filter lookup.
    """
    unique_ids = list(dict.fromkeys(context_ids))
    if not unique_ids:
        return {}
    result = await db.execute(
        select(AgentContextBinding).where(
            AgentContextBinding.agent_id == scope.agent_id,
            AgentContextBinding.context_id.in_(unique_ids),
        )
    )
    filters: dict[uuid.UUID, BindingRowFilter] = {}
    for binding in result.scalars().all():
        row_filter = BindingRowFilter.from_binding(binding)
        if row_filter is not None:
            filters[binding.context_id] = row_filter
    return filters


async def emit_row_filter_would_deny(
    scope: AgentScope,
    *,
    operation: str,
    user_id: str,
    context_id: uuid.UUID,
    denied_memory_ids: list[uuid.UUID],
) -> None:
    """Persist the shadow-mode row-filter aggregate (#1299).

    Row-level filtering in shadow mode keeps every row (the enforcement ramp
    must change nothing observable) and records ONE would_deny row per
    (operation, context) with the denied count and a capped id list — never
    one row per filtered memory (the 4 KB event_metadata CHECK and the audit
    volume both forbid that). The ids are unverified claims, capped at the
    forget-lane precedent ``MAX_METADATA_MEMORY_IDS``.
    """
    if not denied_memory_ids:
        return
    from services.memory_access_event_writer import (
        MAX_METADATA_MEMORY_IDS,
        emit_memory_access_event,
    )

    await emit_memory_access_event(
        operation=operation,
        outcome="success",
        workspace_id=scope.workspace_id,
        user_id=user_id,
        policy_decision=DECISION_WOULD_DENY,
        extra_metadata={
            "requested_context_id": str(context_id),
            "access": ACCESS_READ,
            "filter_kind": ROW_FILTER_KIND,
            "would_deny_count": len(denied_memory_ids),
            "memory_ids": [str(mid) for mid in denied_memory_ids[:MAX_METADATA_MEMORY_IDS]],
        },
    )


async def binding_memory_sql_predicate(db: AsyncSession) -> Any | None:
    """SQL form of the agent-binding read filter over ``Memory`` (#1301).

    For SELECT/aggregate surfaces that never materialize full memory rows
    (the ``/memory/list`` count + page pair, the access-patterns aggregates,
    the stats ``GROUP BY``\\ s), post-filtering with
    :func:`filter_memory_rows_by_binding` would leave the counts acting as an
    existence oracle over denied rows and make pagination drift from
    ``total``. This encodes the read lanes' full subtractive decision as a
    WHERE clause instead — one bulk binding SELECT per request, applied
    identically to the count and the row query:

    - **Context membership (P0-2 default-deny)**: rows outside the agent's
      readable bound set (no binding, or ``can_read=False``) are subtracted.
      The SCOPED forms of these surfaces are already context-gated at the
      ``resolve_context_for_workspace_read`` chokepoint (#1275 uniform 404);
      the membership gate here is what covers the UNSCOPED forms (no
      ``context_id`` → no chokepoint) and cross-context aggregates.
      ``context_id IS NULL`` rows fall out of the ``IN`` gate too
      (fail-closed: a NULL context is not a bound one).
    - **Per-memory type/source subtraction (#1299)**: the same subtraction as
      :meth:`BindingRowFilter.permits`, one clause per restricting binding.

    Returns ``None`` (apply nothing) for non-agent credentials and
    shadow-mode scopes. Shadow here is a pure no-op: these surfaces sit
    outside the MAE operation vocabulary, so the enforcement ramp observes
    would-deny volume through the recall / load_pinned lanes, not here.
    """
    from sqlalchemy import and_, false, not_, or_

    from auth.agent_scope import get_agent_scope
    from models.memory import Memory

    scope = get_agent_scope()
    if scope is None or scope.enforcement_mode == AGENT_ENFORCEMENT_SHADOW:
        return None

    result = await db.execute(
        select(AgentContextBinding).where(AgentContextBinding.agent_id == scope.agent_id)
    )
    bindings = result.scalars().all()

    readable_ids = [binding.context_id for binding in bindings if binding.can_read]
    membership_gate = Memory.context_id.in_(readable_ids) if readable_ids else false()

    denied_clauses = []
    for binding in bindings:
        if not binding.can_read:
            continue  # already excluded by the membership gate
        row_filter = BindingRowFilter.from_binding(binding)
        if row_filter is None:
            continue
        dims = []
        if row_filter.allowed_memory_types is not None:
            allowed_types = sorted(row_filter.allowed_memory_types)
            dims.append(Memory.type.in_(allowed_types) if allowed_types else false())
        if row_filter.allowed_source_types is not None:
            allowed_sources = sorted(row_filter.allowed_source_types)
            dims.append(Memory.source_type.in_(allowed_sources) if allowed_sources else false())
        denied_clauses.append(and_(Memory.context_id == binding.context_id, not_(and_(*dims))))
    if not denied_clauses:
        return membership_gate
    return and_(membership_gate, not_(or_(*denied_clauses)))


# #1366: the run-level aggregate fields withheld from enforce-mode agents.
# Single source of truth shared by BOTH serialization lanes (REST
# ``AnalysisRow.redacted_for_agent_scope`` and MCP ``_serialize_run_row``)
# so a future aggregate added to one lane's redaction cannot silently stay
# exposed on the other.
REDACTED_RUN_AGGREGATE_FIELDS: tuple[str, ...] = (
    "input_count",
    "cost_estimated_cents",
    "cost_actual_cents",
)


def agent_scope_is_enforce() -> bool:
    """True iff the current request's credential is an ENFORCE-mode agent (#1366).

    The cheap (no-DB, contextvar-only) companion to
    :func:`binding_memory_sql_predicate`, sharing its exact activation
    condition: non-agent credentials and shadow-mode scopes return False
    (the enforcement ramp must observe no behavioral change in shadow).
    Serializer-level redaction of aggregate fields (run ``input_count``
    / cost columns) keys off this so the decision cannot drift from the
    SQL lane's.
    """
    from auth.agent_scope import get_agent_scope

    scope = get_agent_scope()
    return scope is not None and scope.enforcement_mode != AGENT_ENFORCEMENT_SHADOW


async def filter_memory_rows_by_binding(
    db: AsyncSession,
    rows: list[Any],
    *,
    operation: str | None,
    user_id: str | None,
) -> tuple[list[Any], int]:
    """Apply the per-memory type/source binding filter to fetched rows (#1299).

    The shared row-materialization lever for every read lane that returns a
    SET of memory rows (recall candidates, load_pinned, explore neighbors,
    declared-link refs, the upcoming time lane) — placing it at the service
    layer is what gives REST and MCP the same behavior by construction
    (#1291/#1292). Rows only need ``id`` / ``context_id`` / ``type`` /
    ``source_type`` attributes. One bulk binding SELECT per call, never
    per-row.

    Returns ``(kept_rows, denied_count)``:

    - **Non-agent credential**: structural no-op (one contextvar read, no
      query).
    - **Enforce**: denied rows are dropped — subtractive; the request still
      succeeds, so no deny row is written here (the caller threads
      ``denied_count`` onto its success emission).
    - **Shadow**: rows are returned UNCHANGED (the enforcement ramp must
      change nothing observable) and ``denied_count`` is 0; when
      ``operation`` is threaded (MAE vocabulary) each affected context lands
      ONE ``would_deny`` aggregate row. Outside the vocabulary (explore,
      time lane) it stays log-only.
    """
    from auth.agent_scope import get_agent_scope

    scope = get_agent_scope()
    if scope is None or not rows:
        return list(rows), 0
    filters = await binding_row_filters_for_contexts(db, scope, [row.context_id for row in rows])
    if not filters:
        return list(rows), 0

    kept: list[Any] = []
    denied_by_context: dict[uuid.UUID, list[uuid.UUID]] = {}
    for row in rows:
        row_filter = filters.get(row.context_id)
        if row_filter is None or row_filter.permits(row.type, row.source_type):
            kept.append(row)
        else:
            denied_by_context.setdefault(row.context_id, []).append(row.id)
    if not denied_by_context:
        return kept, 0

    denied_total = sum(len(ids) for ids in denied_by_context.values())
    if scope.enforcement_mode == AGENT_ENFORCEMENT_SHADOW:
        logger.warning(
            "agent_binding_row_filter_would_deny",
            agent_id=str(scope.agent_id),
            operation=operation,
            denied_count=denied_total,
            context_count=len(denied_by_context),
        )
        if operation is not None and user_id is not None:
            for ctx_id, memory_ids in denied_by_context.items():
                await emit_row_filter_would_deny(
                    scope,
                    operation=operation,
                    user_id=user_id,
                    context_id=ctx_id,
                    denied_memory_ids=memory_ids,
                )
        return list(rows), 0

    logger.warning(
        "agent_binding_row_filter_denied",
        agent_id=str(scope.agent_id),
        operation=operation,
        denied_count=denied_total,
        context_count=len(denied_by_context),
    )
    return kept, denied_total


def _validate_write_policy(value: Any) -> str:
    if value not in _ALL_BINDING_WRITE_POLICIES:
        raise ValidationError(
            f"'write_policy' must be one of {list(_ALL_BINDING_WRITE_POLICIES)}",
            field="write_policy",
        )
    return value


async def agent_binding_permits(
    db: AsyncSession,
    context_id: uuid.UUID,
    access: str,
    *,
    operation: str | None = None,
    user_id: str | None = None,
    requested_memory_id: uuid.UUID | None = None,
    memory_type: str | None = None,
    memory_source_type: str | None = None,
) -> bool:
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

    #1286 (P0-5): ``operation`` / ``user_id`` / ``requested_memory_id`` are
    the audit-identity passthrough — when a memory-op caller threads them,
    the evaluation persists its deny decisions to ``memory_access_events``
    (see :meth:`AgentBindingService.evaluate_context_access`).

    #1299: ``memory_type`` / ``memory_source_type`` are the per-memory row
    passthrough — read-lane callers that hold the memory row thread its own
    type/source so the binding's ``allowed_memory_types`` /
    ``allowed_source_types`` arrays apply. Callers that do not thread them
    keep the context-level-only evaluation, byte-for-byte.
    """
    from auth.agent_scope import get_agent_scope

    scope = get_agent_scope()
    if scope is None:
        return True
    allowed, _decision = await AgentBindingService(db).evaluate_context_access(
        scope,
        context_id,
        access,
        operation=operation,
        user_id=user_id,
        requested_memory_id=requested_memory_id,
        memory_type=memory_type,
        memory_source_type=memory_source_type,
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
        self,
        scope: AgentScope,
        context_id: uuid.UUID,
        access: str,
        *,
        operation: str | None = None,
        user_id: str | None = None,
        requested_memory_id: uuid.UUID | None = None,
        memory_type: str | None = None,
        memory_source_type: str | None = None,
    ) -> tuple[bool, str]:
        """Evaluate the binding intersection for one context.

        Returns ``(allowed, decision)`` where decision ∈ {allowed,
        binding_denied, would_deny}. ``would_deny`` means the binding would
        deny but ``enforcement_mode='shadow'`` lets the request proceed under
        legacy semantics (the migration ramp) — callers MUST treat it as
        allowed. This function can only subtract: it is called strictly AFTER
        the existing RBAC decision allowed the request.

        #1286 (P0-5) deny capture: when the caller threads audit identity
        (``operation`` + ``user_id``), the decision is persisted to
        ``memory_access_events`` — a hard deny as ``outcome='denied'`` /
        ``policy_decision='binding_denied'``; a shadow would-deny as
        ``outcome='success'`` / ``policy_decision='would_deny'`` (the request
        proceeds; the row is the shadow→enforce ramp signal). The requested
        identifiers ride ``event_metadata`` as claims (the authoritative
        ``context_id`` / ``memory_id`` columns stay NULL) and ``workspace_id``
        is the CREDENTIAL scope. When several gates evaluate the SAME denied
        context in one request (pre-gate + service gate), the writer's
        request-scoped dedup collapses the shadow rows to one — every gate
        may emit unconditionally. Hard denies stop the request, so only one
        gate can ever reach its emission.

        #1299 per-memory row filter: read-lane callers that hold the memory
        row thread ``memory_type`` / ``memory_source_type``; when the
        context-level decision permits AND the binding carries type/source
        arrays, the row is additionally checked against them (``NULL`` =
        unrestricted, ``[]`` = deny-all). A row-filter fail takes the same
        enforce/shadow branches as a context-level fail, with
        ``filter_kind='type_source'`` riding the emission so the two deny
        causes stay distinguishable (and the writer dedup keeps both shadow
        signals). Callers that thread nothing keep context-level-only
        semantics, byte-for-byte.
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

        row_filter_denied = False
        if (
            permitted
            and binding is not None
            and (memory_type is not None or memory_source_type is not None)
        ):
            row_filter = BindingRowFilter.from_binding(binding)
            if row_filter is not None and not row_filter.permits(memory_type, memory_source_type):
                permitted = False
                row_filter_denied = True

        if permitted:
            return True, DECISION_ALLOWED

        filter_kind = ROW_FILTER_KIND if row_filter_denied else None

        if scope.enforcement_mode == AGENT_ENFORCEMENT_SHADOW:
            # Shadow ramp: proceed, but record the violation (#1286 item 2 —
            # the durable would_deny row this log line used to promise).
            logger.warning(
                "agent_binding_would_deny",
                agent_id=str(scope.agent_id),
                context_id=str(context_id),
                access=access,
                bound=binding is not None,
                filter_kind=filter_kind,
            )
            await self._emit_decision(
                scope,
                context_id,
                access,
                DECISION_WOULD_DENY,
                operation=operation,
                user_id=user_id,
                requested_memory_id=requested_memory_id,
                filter_kind=filter_kind,
            )
            return True, DECISION_WOULD_DENY

        logger.warning(
            "agent_binding_denied",
            agent_id=str(scope.agent_id),
            context_id=str(context_id),
            access=access,
            bound=binding is not None,
            filter_kind=filter_kind,
        )
        await self._emit_decision(
            scope,
            context_id,
            access,
            DECISION_BINDING_DENIED,
            operation=operation,
            user_id=user_id,
            requested_memory_id=requested_memory_id,
            filter_kind=filter_kind,
        )
        return False, DECISION_BINDING_DENIED

    async def _emit_decision(
        self,
        scope: AgentScope,
        context_id: uuid.UUID,
        access: str,
        decision: str,
        *,
        operation: str | None,
        user_id: str | None,
        requested_memory_id: uuid.UUID | None,
        filter_kind: str | None = None,
    ) -> None:
        """Persist a deny/would-deny decision (#1286 item 2, P0-5).

        No-op unless the caller threaded audit identity — un-threaded
        chokepoints (enumeration surfaces, non-memory ops outside the MAE
        vocabulary) keep the log-only behavior. The writer is fail-open on
        its own independent session, so this can never break the request.
        """
        if operation is None or user_id is None:
            return
        from services.memory_access_event_writer import emit_memory_access_event

        metadata: dict[str, str] = {
            "requested_context_id": str(context_id),
            "access": access,
        }
        if requested_memory_id is not None:
            metadata["requested_memory_id"] = str(requested_memory_id)
        if filter_kind is not None:
            metadata["filter_kind"] = filter_kind
        await emit_memory_access_event(
            operation=operation,
            # Shadow proceeds — the request outcome IS success; the ramp
            # signal rides policy_decision.
            outcome="denied" if decision == DECISION_BINDING_DENIED else "success",
            workspace_id=scope.workspace_id,
            user_id=user_id,
            policy_decision=decision,
            extra_metadata=metadata,
        )

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
