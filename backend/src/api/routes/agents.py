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

from auth.dependencies import require_workspace_admin
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
