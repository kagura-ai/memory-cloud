"""Agent bootstrap composition service (RFC-0002 P0-3, Issue #1276).

``get_agent_bootstrap`` is the single session-start call that rehydrates an
agent's cognitive state by **composing existing primitives** — it adds no
parallel retrieval path and no second scoring surface (F2 design invariant 1,
``docs/design/agent-bootstrap-contract.md``). Each component delegates to the
exact service chokepoint the standalone primitive uses, so bounds, ordering,
trust filtering, ranking, and IDOR posture are inherited, not re-specified.

Shared by the MCP tool (``mcp_server/tools/agent_bootstrap.py``) and the REST
companion (``POST /api/v1/agents/{agent_id}/bootstrap``).

Total, fail-closed errors (identity/authorization) raise ``BootstrapError``;
component failures are fail-soft — a failing component yields
``{"status": "error", ...}`` for that component only, with top-level
``degraded: true``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from utils.datetime import to_utc_iso, utcnow
from utils.logger import get_logger

logger = get_logger(__name__)

# Component health vocabulary (per-component; distinct from the top-level
# house envelope status which stays "success").
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

_ALL_COMPONENTS = ("pinned", "recall", "upcoming", "state", "policy")
_QUERY_MAX_LEN = 1024
_SESSION_ID_MAX_LEN = 128


class BootstrapError(Exception):
    """A total, fail-closed bootstrap failure (identity/authorization/args).

    ``code`` is the snake_case ``_error_response`` code the surfaces render;
    ``message`` is the generic caller-facing string (never leaks existence).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class BootstrapParams:
    """Validated request parameters (surface-agnostic)."""

    agent_id: UUID
    context_id: UUID | None = None
    session_id: str | None = None
    query: str | None = None
    recall_k: int | None = None
    pinned_cap: int | None = None
    upcoming_until: str | None = None
    include: tuple[str, ...] = _ALL_COMPONENTS


@dataclass
class BootstrapPrincipal:
    """Who is bootstrapping — an agent-bound key, or an owner/admin operator."""

    user_id: str
    workspace_id: UUID
    principal_type: str  # "agent" | "owner" | "admin"
    on_behalf_of: str | None = None  # set for non-agent (operator) bootstraps
    # The pure API-key workspace scope (#963), forwarded to the resolver.
    key_workspace_id: UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def parse_include(raw: Any) -> tuple[str, ...]:
    """Validate the optional ``include`` selector; default = all components."""
    if raw is None:
        return _ALL_COMPONENTS
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise BootstrapError("invalid_arguments", "'include' must be a list of strings.")
    unknown = sorted(set(raw) - set(_ALL_COMPONENTS))
    if unknown:
        raise BootstrapError(
            "invalid_arguments",
            f"'include' has unknown components {unknown} (valid: {list(_ALL_COMPONENTS)}).",
        )
    # Preserve declared order, dedup.
    return tuple(c for c in _ALL_COMPONENTS if c in raw)


def validate_session_id(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or len(raw) > _SESSION_ID_MAX_LEN:
        raise BootstrapError(
            "invalid_arguments",
            f"'session_id' must be a string of at most {_SESSION_ID_MAX_LEN} chars.",
        )
    if raw and not all(c.isalnum() or c in "._-" for c in raw):
        raise BootstrapError("invalid_arguments", "'session_id' allows only [A-Za-z0-9._-].")
    return raw or None


def validate_query(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or len(raw) > _QUERY_MAX_LEN:
        raise BootstrapError(
            "invalid_arguments",
            f"'query' must be a string of at most {_QUERY_MAX_LEN} chars.",
        )
    return raw or None


class AgentBootstrapService:
    """Compose the agent bootstrap envelope from existing primitives."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def resolve_principal_and_agent(
        self,
        *,
        requested_agent_id: UUID,
        user: dict,
        agent_scope: Any,
    ) -> tuple[BootstrapPrincipal, Any]:
        """Apply the F2 identity rule and resolve the agent (fail-closed).

        ``agent_scope`` is the per-request AgentScope (or None). Raises
        ``BootstrapError('agent_not_found')`` uniformly for both "no such
        agent" and "not yours" (no existence oracle).
        """
        from services.agent_registry_service import AgentRegistryService

        user_id = user.get("user_id")
        if not user_id:
            raise BootstrapError("agent_not_found", "Agent not found.")

        if agent_scope is not None:
            # Agent-bound key: the requested agent MUST equal the key's agent.
            if requested_agent_id != agent_scope.agent_id:
                raise BootstrapError("agent_not_found", "Agent not found.")
            workspace_id = _coerce_uuid(user.get("current_workspace_id"))
            key_workspace_id = _coerce_uuid(user.get("api_key_workspace_id"))
            if workspace_id is None:
                raise BootstrapError("agent_not_found", "Agent not found.")
            agent = await AgentRegistryService(self.db).get_agent(workspace_id, requested_agent_id)
            if agent is None:
                raise BootstrapError("agent_not_found", "Agent not found.")
            return (
                BootstrapPrincipal(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    principal_type="agent",
                    key_workspace_id=key_workspace_id,
                    metadata={"principal_type": "agent"},
                ),
                agent,
            )

        # Non-agent credential: only a workspace owner/admin may bootstrap an
        # agent of their workspace (e.g. for testing), recorded as on_behalf_of.
        workspace_id = _coerce_uuid(user.get("current_workspace_id"))
        if workspace_id is None:
            raise BootstrapError("agent_not_found", "Agent not found.")
        agent = await AgentRegistryService(self.db).get_agent(workspace_id, requested_agent_id)
        if agent is None:
            raise BootstrapError("agent_not_found", "Agent not found.")

        from services.permission_service import PermissionService
        from utils.exceptions import AuthorizationError

        try:
            member = await PermissionService(self.db).check_workspace_admin(user_id, workspace_id)
        except AuthorizationError as exc:
            # Not owner/admin — uniform agent_not_found (no existence oracle).
            raise BootstrapError("agent_not_found", "Agent not found.") from exc

        role = getattr(member, "role", "admin")
        principal_type = "owner" if str(role) in ("owner", "WorkspaceRole.OWNER") else "admin"
        return (
            BootstrapPrincipal(
                user_id=user_id,
                workspace_id=workspace_id,
                principal_type=principal_type,
                on_behalf_of=user_id,
                key_workspace_id=_coerce_uuid(user.get("api_key_workspace_id")),
                metadata={"principal_type": principal_type, "on_behalf_of": user_id},
            ),
            agent,
        )

    async def resolve_context(
        self, *, agent: Any, params: BootstrapParams, principal: BootstrapPrincipal
    ) -> tuple[Any, dict[str, Any]]:
        """Resolve the bootstrap context + its binding descriptor (fail-closed).

        Returns ``(context, binding_info)`` where binding_info is
        ``{context_id, is_default}``. Raises ``BootstrapError`` with
        ``context_id_required`` / ``context_not_found`` per F2.
        """
        from services.agent_binding_service import AgentBindingService
        from services.permission_service import PermissionService
        from utils.exceptions import NotFoundException

        binding_service = AgentBindingService(self.db)
        context_id = params.context_id
        is_default = False

        if context_id is None:
            binding, outcome = await binding_service.resolve_default_binding(agent.id)
            if binding is None:
                # none / ambiguous — do NOT enumerate bindings (no oracle).
                raise BootstrapError(
                    "context_id_required",
                    "context_id is required — the agent has no default binding.",
                )
            context_id = binding.context_id
            is_default = binding.is_default
        else:
            existing = await binding_service.get_binding_for_context(agent.id, context_id)
            is_default = bool(existing and existing.is_default)

        # Authorization is additive on the existing chain: uniform-404 resolver
        # first (with #963 key-workspace forwarding + allowed_context_ids), and
        # the AgentContextBinding intersection is applied inside it because the
        # per-request agent scope is set — it MAY narrow, MUST NOT widen.
        try:
            context = await PermissionService(self.db).resolve_context_for_workspace_read(
                user_id=principal.user_id,
                context_id=context_id,
                key_workspace_id=principal.key_workspace_id,
            )
        except NotFoundException as exc:
            raise BootstrapError("context_not_found", "Context not found.") from exc

        return context, {"context_id": str(context_id), "is_default": is_default}

    async def build_envelope(
        self,
        *,
        agent: Any,
        context: Any,
        binding_info: dict[str, Any],
        params: BootstrapParams,
        principal: BootstrapPrincipal,
        recall_metered: bool,
    ) -> dict[str, Any]:
        """Compose the response envelope with per-component fail-soft.

        ``recall_metered`` is True when the recall component (query present)
        exceeded the caller's rate budget — the component degrades to
        ``rate_limited`` while the cheap components still return.
        """
        components: dict[str, Any] = {}
        degraded = False

        # context block + instructions (byte-compatible with get_context_info).
        context_block, instructions = await self._context_and_instructions(context)

        include = set(params.include)

        if "pinned" in include:
            components["pinned"] = await self._component(
                "pinned", lambda: self._pinned(context, principal)
            )
        if "recall" in include:
            components["recall"] = await self._recall_component(
                context, params, principal, recall_metered
            )
        if "upcoming" in include:
            components["upcoming"] = await self._component(
                "upcoming", lambda: self._upcoming(context, params)
            )
        if "state" in include:
            components["state"] = await self._component("state", lambda: self._state(context))
        if "policy" in include:
            components["policy"] = {"status": STATUS_SKIPPED, "reason": "no_policy_bundle"}

        degraded = any(c.get("status") == STATUS_ERROR for c in components.values())

        return {
            "status": "success",
            "degraded": degraded,
            "agent": {
                "agent_id": str(agent.id),
                "name": agent.name,
                "binding": binding_info,
            },
            "context": context_block,
            "instructions": instructions,
            "components": components,
            "correlation": self._correlation_block(agent, params),
            "generated_at": to_utc_iso(utcnow()),
        }

    # ------------------------------------------------------------------
    # Components
    # ------------------------------------------------------------------

    async def _component(self, name: str, fn: Any) -> dict[str, Any]:
        """Run one component fail-soft: {status: ok, ...} or {status: error}.

        Each component runs inside a SAVEPOINT (``begin_nested``): a genuine
        Postgres-level error in one component (lock timeout, transient blip)
        rolls back ONLY that component's partial work and leaves the shared
        session usable, so the remaining components — and the final commit —
        do not hit ``PendingRollbackError``. Without the savepoint, a single
        component's DB error would poison the transaction and deny the agent
        everything, defeating the fail-soft design (the ``get_db()``
        auto-commit trap; inner code review). The #1255 per-event SAVEPOINT
        pattern.
        """
        try:
            async with self.db.begin_nested():
                body = await fn()
            return {"status": STATUS_OK, **body}
        except Exception as exc:  # fail-soft per component; savepoint rolled back
            logger.error(f"bootstrap_component_failed: {name}: {exc}", exc_info=True)
            return {"status": STATUS_ERROR, "error": "component_error"}

    def _correlation_block(self, agent: Any, params: BootstrapParams) -> dict[str, Any]:
        """Build the correlation block, populating trace/span from the P0-4
        (#1277) per-request correlation context when present.

        ``session_id`` prefers the explicit bootstrap arg (functional input),
        falling back to the baggage-derived session id.
        """
        from api.correlation import get_correlation

        corr = get_correlation()
        return {
            "agent_id": str(agent.id),
            "session_id": params.session_id or (corr.session_id if corr else None),
            "run_id": corr.run_id if corr else None,
            "trace_id": corr.trace_id if corr else None,
            "span_id": corr.span_id if corr else None,
        }

    async def audit_on_behalf_of(
        self, *, agent: Any, principal: BootstrapPrincipal, session_id: str | None
    ) -> None:
        """Write the on-behalf-of audit row for an operator bootstrap (#1276).

        F2 normative: a non-agent (owner/admin) credential bootstrapping an
        agent MUST record ``on_behalf_of`` + ``principal_type`` so operator
        activity is distinguishable from the agent's own and cannot masquerade
        as it. No-op for agent-bound calls (``on_behalf_of`` is None — the
        activity IS the agent's, covered by usage + correlation). Added to the
        session but NOT committed — the caller commits atomically.
        """
        if not principal.on_behalf_of:
            return
        from services.agent_registry_service import (
            AUDIT_AGENT_BOOTSTRAP_ON_BEHALF,
            add_agent_audit_row,
        )

        add_agent_audit_row(
            self.db,
            actor_user_id=principal.user_id,
            actor_email=None,
            action=AUDIT_AGENT_BOOTSTRAP_ON_BEHALF,
            agent_id=agent.id,
            workspace_id=principal.workspace_id,
            metadata={
                "principal_type": principal.principal_type,
                "on_behalf_of": principal.on_behalf_of,
                "session_id": session_id,
            },
        )

    async def _context_and_instructions(self, context: Any) -> tuple[dict[str, Any], str]:
        from sqlalchemy import select

        from config.settings import get_settings
        from mcp_server.tools._constants import KAGURA_MEMORY_INSTRUCTIONS
        from models.config import ContextSearchConfig

        settings = get_settings()
        config = (
            await self.db.execute(
                select(ContextSearchConfig).where(ContextSearchConfig.context_id == context.id)
            )
        ).scalar_one_or_none()

        usage_guide = context.usage_guide or (
            "No usage guide provided. Please add usage guidelines in the context settings."
        )
        context_block = {
            "id": str(context.id),
            "name": context.name,
            "display_name": context.display_name,
            "summary": context.summary
            or "No summary provided. Please add a summary in the context settings.",
            "usage_guide": usage_guide,
            "is_private": context.is_private,
            "is_locked": context.is_locked,
            "embedding_model": config.embedding_model if config else settings.embedding_model,
            "embedding_dimensions": config.embedding_dimensions
            if config
            else settings.embedding_dimensions,
        }
        # instructions = context usage_guide, blank line, standard instructions
        # (the get_context_info precedent — usage_guide first).
        instructions = f"{usage_guide}\n\n{KAGURA_MEMORY_INSTRUCTIONS}"
        return context_block, instructions

    async def _pinned(self, context: Any, principal: BootstrapPrincipal) -> dict[str, Any]:
        from services.memory_service import MemoryService

        result = await MemoryService(self.db).load_pinned(
            user_id=principal.user_id,
            current_context_id=context.id,
            current_workspace_id=principal.workspace_id,
            cap=None,  # inherit the load_pinned default clamp
        )
        return {
            "memories": [
                {
                    "memory_id": str(m.memory_id),
                    "summary": m.summary,
                    "context_summary": m.context_summary,
                    "type": m.type,
                    "importance": m.importance,
                    "delivery_mode": m.delivery_mode,
                }
                for m in result.memories
            ],
            "total_available": result.total_available,
            "truncated": result.truncated,
            "cap": result.cap,
        }

    async def _recall_component(
        self,
        context: Any,
        params: BootstrapParams,
        principal: BootstrapPrincipal,
        recall_metered: bool,
    ) -> dict[str, Any]:
        # Server never fabricates a query — absent query = skipped, even when
        # include explicitly names recall (F2 normative).
        if params.query is None:
            return {"status": STATUS_SKIPPED, "reason": "no_query"}
        if recall_metered:
            return {"status": STATUS_ERROR, "error": "rate_limited"}
        return await self._component("recall", lambda: self._recall(context, params, principal))

    async def _recall(
        self, context: Any, params: BootstrapParams, principal: BootstrapPrincipal
    ) -> dict[str, Any]:
        from config.settings import get_settings
        from models.schemas import RecallRequest
        from services.memory_service import MemoryService
        from utils.hashing import hmac_sha256_hex

        # Trusted-tier-only recall — bootstrap output is behaviour-establishing
        # (OWASP LLM01/LLM03); not configurable in v1 (F2 invariant 3).
        request = RecallRequest(
            query=params.query,
            k=params.recall_k if params.recall_k is not None else 5,
            filters={"trust_tier": "trusted"},
        )
        result = await MemoryService(self.db).recall(
            request,
            user_id=principal.user_id,
            current_context_id=context.id,
            current_workspace_id=principal.workspace_id,
            context_workspace_id=context.workspace_id,
        )
        results_data = [
            {
                "memory_id": str(r.memory_id),
                "summary": r.summary,
                "context_summary": r.context_summary,
                "type": r.type,
                "importance": r.importance,
                "scope": r.scope,
                "score": r.score,
                "tags": r.tags,
                "created_at": to_utc_iso(r.created_at),
                "updated_at": to_utc_iso(r.updated_at),
                "superseded_by": str(r.superseded_by) if r.superseded_by else None,
                "contradicts": [str(c) for c in r.contradicts],
            }
            for r in result.results
        ]
        # query_hash never leaks the raw query — correlation only.
        query_hash = hmac_sha256_hex(params.query or "", get_settings().api_key_secret)
        return {
            "query_hash": query_hash,
            "results": results_data,
            "k": request.k,
            "trust_filter": "trusted",
        }

    async def _upcoming(self, context: Any, params: BootstrapParams) -> dict[str, Any]:
        from services.time_memory import query_upcoming_time_memories
        from utils.time_trigger import parse_query_bound

        q_from = parse_query_bound("now")
        q_until = parse_query_bound(params.upcoming_until) if params.upcoming_until else None
        results = await query_upcoming_time_memories(
            self.db, context.id, q_from=q_from, q_until=q_until, k=20
        )
        return {"results": results, "from": q_from, "until": q_until}

    async def _state(self, context: Any) -> dict[str, Any]:
        from services.agent_state_service import AgentStateService

        states = await AgentStateService(self.db).list_state(context.id)
        return {"states": states, "count": len(states)}


def _coerce_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None
