"""Agent Registry REST routes (RFC-0002 P0-1, Issue #1274).

Owner/admin-gated CRUD over the workspace-scoped ``agents`` table. The flat
``/api/v1/agents`` namespace matches the F2 bootstrap companion
(``POST /api/v1/agents/{agent_id}/bootstrap``, P0-3); the workspace boundary
comes from the authenticated principal's active workspace, mirroring the
other workspace-surface routes.

Every mutation writes an ``audit_logs`` row via the security-mutation lane
(``services.agent_registry_service.add_agent_audit_row``) and commits it
atomically with the mutation. ``enforce`` → ``shadow`` transitions are
recorded under the distinct ``agent_enforcement_widened`` action.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import APIKeyOrSessionUser, require_workspace_admin
from db.base import get_db
from models.agent import Agent
from models.api_base import TZAwareBaseModel
from services.agent_registry_service import (
    AUDIT_AGENT_DELETED,
    AUDIT_AGENT_ENFORCEMENT_WIDENED,
    AUDIT_AGENT_REGISTERED,
    AUDIT_AGENT_UPDATED,
    AgentRegistryService,
    add_agent_audit_row,
    enforcement_widened,
)
from utils.exceptions import NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


# ============================================================================
# Pydantic models
# ============================================================================


class AgentCreate(BaseModel):
    """Request model for registering an agent."""

    name: str = Field(..., min_length=1, max_length=255, description="Workspace-unique name")
    description: str | None = Field(None, max_length=10_000)
    framework: str | None = Field(
        None, max_length=100, description="Free-form: 'claude-code', 'langgraph', ..."
    )
    environment: str | None = Field(
        None, max_length=100, description="Aligned with OTel deployment.environment.name"
    )
    version: str | None = Field(
        None, max_length=100, description="Agent build/prompt version (client-reported)"
    )


class AgentUpdate(BaseModel):
    """Request model for a partial agent update.

    Only fields explicitly present in the request body are applied
    (``exclude_unset``), so ``null`` clears a metadata field while an absent
    key leaves it untouched.
    """

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=10_000)
    framework: str | None = Field(None, max_length=100)
    environment: str | None = Field(None, max_length=100)
    version: str | None = Field(None, max_length=100)
    status: Literal["active", "suspended", "retired"] | None = Field(
        None, description="Lifecycle kill switch (fail-closed for bound keys, P0-2)"
    )
    enforcement_mode: Literal["shadow", "enforce"] | None = Field(
        None, description="Binding enforcement ramp; enforce→shadow is audited as widening"
    )


class AgentResponse(TZAwareBaseModel):
    """Response model for one registry row."""

    id: UUID
    workspace_id: UUID
    name: str
    description: str | None = None
    owner_user_id: str
    framework: str | None = None
    environment: str | None = None
    version: str | None = None
    status: str
    enforcement_mode: str
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentListResponse(BaseModel):
    """Response model for the workspace agent listing."""

    agents: list[AgentResponse]
    count: int


# ============================================================================
# Helpers
# ============================================================================


def _actor(user: dict) -> tuple[str, str | None, UUID, dict[str, Any]]:
    """Extract (user_id, email, workspace_id, audit-base-metadata) from auth.

    ``via`` discriminates on the ``api_key_workspace_id`` KEY (present on
    every API-key principal dict, including global keys where its value is
    None) rather than on ``api_key_prefix`` truthiness — a programmatic call
    whose prefix did not surface must still be attributed as ``api_key``,
    not misclassified as ``session`` (Copilot review, PR #1279).
    """
    user_id = user["user_id"]
    workspace_id = UUID(str(user["current_workspace_id"]))
    metadata: dict[str, Any] = {
        "via": "api_key" if "api_key_workspace_id" in user else "session",
    }
    if user.get("api_key_prefix"):
        metadata["key_prefix"] = user["api_key_prefix"]
    return user_id, user.get("email"), workspace_id, metadata


async def _get_agent_or_404(
    service: AgentRegistryService, workspace_id: UUID, agent_id: UUID
) -> Agent:
    agent = await service.get_agent(workspace_id, agent_id)
    if agent is None:
        # Uniform not-found inside the workspace boundary (the workspace
        # filter in get_agent is the IDOR guard).
        raise NotFoundException("Agent")
    return agent


# ============================================================================
# Routes
# ============================================================================


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
async def register_agent(
    data: AgentCreate,
    user: dict = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Register an agent in the active workspace (owner/admin only)."""
    user_id, email, workspace_id, metadata = _actor(user)
    service = AgentRegistryService(db)
    agent = await service.create_agent(
        workspace_id=workspace_id,
        name=data.name,
        owner_user_id=user_id,
        description=data.description,
        framework=data.framework,
        environment=data.environment,
        version=data.version,
    )
    add_agent_audit_row(
        db,
        actor_user_id=user_id,
        actor_email=email,
        action=AUDIT_AGENT_REGISTERED,
        agent_id=agent.id,
        workspace_id=workspace_id,
        metadata={**metadata, "agent_name": agent.name},
    )
    await db.commit()
    await db.refresh(agent)
    return agent


@router.get("", response_model=AgentListResponse)
async def list_agents(
    user: dict = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """List the active workspace's registered agents (owner/admin only)."""
    _, _, workspace_id, _ = _actor(user)
    agents = await AgentRegistryService(db).list_agents(workspace_id)
    return AgentListResponse(
        agents=[AgentResponse.model_validate(a) for a in agents],
        count=len(agents),
    )


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: UUID,
    user: dict = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Fetch one registered agent (owner/admin only)."""
    _, _, workspace_id, _ = _actor(user)
    return await _get_agent_or_404(AgentRegistryService(db), workspace_id, agent_id)


@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: UUID,
    data: AgentUpdate,
    user: dict = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Partially update an agent, including status / enforcement transitions."""
    user_id, email, workspace_id, metadata = _actor(user)
    service = AgentRegistryService(db)
    agent = await _get_agent_or_404(service, workspace_id, agent_id)

    updates = data.model_dump(exclude_unset=True)
    # `name=null` etc. are Pydantic-accepted bodies but these columns are
    # non-nullable — reject explicitly instead of letting the service's
    # isinstance check produce a less specific message.
    for non_nullable in ("name", "status", "enforcement_mode"):
        if non_nullable in updates and updates[non_nullable] is None:
            raise ValidationError(f"'{non_nullable}' cannot be null.", field=non_nullable)

    changes = await service.update_agent(agent, updates)
    if changes:
        widened = enforcement_widened(changes)
        transition = changes.get("enforcement_mode") or changes.get("status")
        add_agent_audit_row(
            db,
            actor_user_id=user_id,
            actor_email=email,
            action=AUDIT_AGENT_ENFORCEMENT_WIDENED if widened else AUDIT_AGENT_UPDATED,
            agent_id=agent.id,
            workspace_id=workspace_id,
            metadata={**metadata, "agent_name": agent.name, "changes": changes},
            old_value=transition.get("old") if transition else None,
            new_value=transition.get("new") if transition else None,
        )
        await db.commit()
        await db.refresh(agent)
    return agent


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: UUID,
    user: dict = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Hard-delete a registry row (owner/admin only).

    Operational retirement should normally use ``status='retired'``; hard
    delete stays available and (from P0-2 on) cascades every bound key.
    """
    user_id, email, workspace_id, metadata = _actor(user)
    service = AgentRegistryService(db)
    agent = await _get_agent_or_404(service, workspace_id, agent_id)

    add_agent_audit_row(
        db,
        actor_user_id=user_id,
        actor_email=email,
        action=AUDIT_AGENT_DELETED,
        agent_id=agent.id,
        workspace_id=workspace_id,
        metadata={**metadata, "agent_name": agent.name},
    )
    await service.delete_agent(agent)
    await db.commit()


# ============================================================================
# Context bindings (RFC-0002 P0-2, Issue #1275)
# ============================================================================


class BindingCreate(BaseModel):
    """Request model for binding an agent to a context (subtractive scoping)."""

    context_id: UUID
    can_read: bool = True
    write_policy: Literal["deny", "direct"] = "deny"
    is_default: bool = Field(False, description="Bootstrap default binding (max one per agent)")
    allowed_memory_types: list[str] | None = Field(
        None,
        description="Reserved for #1286; only null is accepted until per-memory enforcement ships",
    )
    allowed_source_types: list[str] | None = Field(
        None,
        description="Reserved for #1286; only null is accepted until per-memory enforcement ships",
    )


class BindingUpdate(BaseModel):
    """Partial binding update (``context_id`` is immutable — recreate to re-target)."""

    can_read: bool | None = None
    write_policy: Literal["deny", "direct"] | None = None
    is_default: bool | None = None
    allowed_memory_types: list[str] | None = Field(
        None, description="Reserved for #1286; only null is currently accepted"
    )
    allowed_source_types: list[str] | None = Field(
        None, description="Reserved for #1286; only null is currently accepted"
    )


class BindingResponse(TZAwareBaseModel):
    """Response model for one binding row."""

    id: UUID
    agent_id: UUID
    context_id: UUID
    can_read: bool
    write_policy: str
    is_default: bool
    allowed_memory_types: list[str] | None = None
    allowed_source_types: list[str] | None = None
    created_by: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BindingListResponse(BaseModel):
    """Response model for an agent's binding listing."""

    bindings: list[BindingResponse]
    count: int


def _binding_service(db: AsyncSession):
    from services.agent_binding_service import AgentBindingService

    return AgentBindingService(db)


@router.post(
    "/{agent_id}/bindings", response_model=BindingResponse, status_code=status.HTTP_201_CREATED
)
async def create_agent_binding(
    agent_id: UUID,
    data: BindingCreate,
    user: dict = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Bind an agent to a context (owner/admin only, purely subtractive)."""
    from services.agent_binding_service import AUDIT_BINDING_CREATED

    user_id, email, workspace_id, metadata = _actor(user)
    agent = await _get_agent_or_404(AgentRegistryService(db), workspace_id, agent_id)

    binding = await _binding_service(db).create_binding(
        agent=agent,
        context_id=data.context_id,
        created_by=user_id,
        can_read=data.can_read,
        write_policy=data.write_policy,
        is_default=data.is_default,
        allowed_memory_types=data.allowed_memory_types,
        allowed_source_types=data.allowed_source_types,
    )
    add_agent_audit_row(
        db,
        actor_user_id=user_id,
        actor_email=email,
        action=AUDIT_BINDING_CREATED,
        agent_id=agent.id,
        workspace_id=workspace_id,
        metadata={
            **metadata,
            "agent_name": agent.name,
            "context_id": str(data.context_id),
            "binding_id": str(binding.id),
            "can_read": binding.can_read,
            "write_policy": binding.write_policy,
            "is_default": binding.is_default,
        },
    )
    await db.commit()
    await db.refresh(binding)
    return binding


@router.get("/{agent_id}/bindings", response_model=BindingListResponse)
async def list_agent_bindings(
    agent_id: UUID,
    user: dict = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """List an agent's context bindings (owner/admin only)."""
    _, _, workspace_id, _ = _actor(user)
    agent = await _get_agent_or_404(AgentRegistryService(db), workspace_id, agent_id)
    bindings = await _binding_service(db).list_bindings(agent)
    return BindingListResponse(
        bindings=[BindingResponse.model_validate(b) for b in bindings],
        count=len(bindings),
    )


@router.patch("/{agent_id}/bindings/{binding_id}", response_model=BindingResponse)
async def update_agent_binding(
    agent_id: UUID,
    binding_id: UUID,
    data: BindingUpdate,
    user: dict = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Partially update a binding (owner/admin only)."""
    from services.agent_binding_service import AUDIT_BINDING_UPDATED

    user_id, email, workspace_id, metadata = _actor(user)
    service = _binding_service(db)
    agent = await _get_agent_or_404(AgentRegistryService(db), workspace_id, agent_id)
    binding = await service.get_binding(agent, binding_id)
    if binding is None:
        raise NotFoundException("Binding")

    updates = data.model_dump(exclude_unset=True)
    for non_nullable in ("can_read", "write_policy", "is_default"):
        if non_nullable in updates and updates[non_nullable] is None:
            raise ValidationError(f"'{non_nullable}' cannot be null.", field=non_nullable)

    changes = await service.update_binding(binding, updates)
    if changes:
        add_agent_audit_row(
            db,
            actor_user_id=user_id,
            actor_email=email,
            action=AUDIT_BINDING_UPDATED,
            agent_id=agent.id,
            workspace_id=workspace_id,
            metadata={
                **metadata,
                "agent_name": agent.name,
                "context_id": str(binding.context_id),
                "binding_id": str(binding.id),
                "changes": changes,
            },
        )
        await db.commit()
        await db.refresh(binding)
    return binding


@router.delete("/{agent_id}/bindings/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_binding(
    agent_id: UUID,
    binding_id: UUID,
    user: dict = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a binding (owner/admin only) — the agent loses that context."""
    from services.agent_binding_service import AUDIT_BINDING_DELETED

    user_id, email, workspace_id, metadata = _actor(user)
    service = _binding_service(db)
    agent = await _get_agent_or_404(AgentRegistryService(db), workspace_id, agent_id)
    binding = await service.get_binding(agent, binding_id)
    if binding is None:
        raise NotFoundException("Binding")

    add_agent_audit_row(
        db,
        actor_user_id=user_id,
        actor_email=email,
        action=AUDIT_BINDING_DELETED,
        agent_id=agent.id,
        workspace_id=workspace_id,
        metadata={
            **metadata,
            "agent_name": agent.name,
            "context_id": str(binding.context_id),
            "binding_id": str(binding.id),
        },
    )
    await service.delete_binding(binding)
    await db.commit()


# ============================================================================
# Bootstrap companion (RFC-0002 P0-3, Issue #1276)
# ============================================================================


class BootstrapRequest(BaseModel):
    """Body for POST /api/v1/agents/{agent_id}/bootstrap (agent_id from path).

    POST-for-read follows the POST /api/v1/memory/pinned precedent.
    """

    context_id: UUID | None = None
    session_id: str | None = Field(None, max_length=128)
    query: str | None = Field(None, max_length=1024)
    recall_k: int | None = None
    pinned_cap: int | None = None
    upcoming_until: str | None = None
    include: list[Literal["pinned", "recall", "upcoming", "state", "policy"]] | None = None


@router.post("/{agent_id}/bootstrap")
async def agent_bootstrap(
    agent_id: UUID,
    body: BootstrapRequest,
    user: APIKeyOrSessionUser,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Rehydrate an agent's cognitive state at session start (#1276).

    Auth is ``APIKeyOrSessionUser``; the per-request agent scope (set at
    API-key verify for agent-bound keys) drives the identity rule inside the
    service. Component failures are fail-soft; identity/authorization failures
    are total and fail-closed with the uniform ``agent_not_found`` /
    ``context_not_found`` shapes.
    """
    from auth.agent_scope import get_agent_scope
    from services.agent_bootstrap_service import (
        AgentBootstrapService,
        BootstrapError,
        BootstrapParams,
        parse_include,
        validate_query,
        validate_session_id,
    )
    from utils.exceptions import BadRequestError, NotFoundException

    try:
        params = BootstrapParams(
            agent_id=agent_id,
            context_id=body.context_id,
            # #1281 item 6: reuse the shared MCP validators so the REST surface
            # enforces the same session_id charset ([A-Za-z0-9._-]) and query
            # limits as the tool contract — Pydantic max_length alone let spaces
            # and other chars through. Both raise BootstrapError (caught below).
            session_id=validate_session_id(body.session_id),
            query=validate_query(body.query),
            recall_k=body.recall_k,
            pinned_cap=body.pinned_cap,
            upcoming_until=body.upcoming_until,
            include=parse_include(body.include),
        )
    except BootstrapError as e:
        raise BadRequestError(message=e.message, error_code=e.code.upper()) from e

    service = AgentBootstrapService(db)
    try:
        principal, agent = await service.resolve_principal_and_agent(
            requested_agent_id=agent_id, user=user, agent_scope=get_agent_scope()
        )
        context, binding_info = await service.resolve_context(
            agent=agent, params=params, principal=principal
        )
    except BootstrapError as e:
        await db.rollback()
        if e.code in ("agent_not_found", "context_not_found"):
            # Uniform 404 (CWE-639) — nonexistent and not-yours are the same.
            raise NotFoundException("Agent" if e.code == "agent_not_found" else "Context") from e
        raise BadRequestError(message=e.message, error_code=e.code.upper()) from e

    # REST recall metering: the recall component runs under the caller's plan
    # limits; a query-carrying bootstrap that trips the limit degrades that
    # component to rate_limited while the cheap components still return.
    recall_metered = False
    if params.query is not None and principal.workspace_id is not None:
        from services.quota_service import QuotaService

        try:
            allowed, _used, _limit = await QuotaService(db).check_mcp_rate_limit(
                principal.workspace_id
            )
            recall_metered = not allowed
        except Exception:
            recall_metered = False

    envelope = await service.build_envelope(
        agent=agent,
        context=context,
        binding_info=binding_info,
        params=params,
        principal=principal,
        recall_metered=recall_metered,
    )
    # #1276: record an operator (owner/admin) "on behalf of" bootstrap so it
    # cannot masquerade as the agent's own activity (no-op for agent-bound).
    await service.audit_on_behalf_of(agent=agent, principal=principal, session_id=params.session_id)
    await db.commit()

    # #1278: append-only audit row (independent session, fail-open, no-op
    # unless the request carries verified agent identity).
    from services.memory_access_event_writer import emit_memory_access_event

    await emit_memory_access_event(
        operation="bootstrap",
        outcome="partial" if envelope.get("degraded") else "success",
        workspace_id=principal.workspace_id,
        user_id=principal.user_id,
        context_id=context.id,
        policy_decision=principal.metadata.get("policy_decision"),
    )
    return envelope
