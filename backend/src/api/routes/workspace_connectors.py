"""Workspace connector setup API routes (Issue #851, F6-b of #755)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceAdmin
from db.base import get_db
from models.api_base import TZAwareBaseModel
from models.schemas import validate_pii_guardrail_config
from services.connector_provisioning import ConnectorProvisioningService
from utils.exceptions import MemoryCloudException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/workspace-connectors", tags=["workspace-connectors"])


class WorkspaceConnectorCreateRequest(BaseModel):
    """Request body for provisioning an ai-worker connector."""

    connector_type: Literal["slack", "discord", "teams"] = Field(
        ..., description="Connector backend to provision"
    )
    resource_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Workspace-scoped resource slug for ingested chat events",
    )
    display_name: str | None = Field(None, max_length=255)
    oauth_tokens: dict[str, Any] | None = Field(
        None, description="OAuth token bundle; stored Fernet-encrypted"
    )
    pii_guardrail_config: dict[str, Any] | None = Field(
        None, description="PII guardrail config for ai-worker pre-compile"
    )
    litellm_virtual_key_id: str | None = Field(None, max_length=255)
    virtual_key_valid_until: datetime | None = None
    quota_events_per_hour: int = Field(1000, ge=1, le=10000)
    # Spec 2026-06-02 registration flow (all optional, backward-compatible).
    context_id: UUID | None = Field(None, description="Existing write-target context")
    auto_create_context_name: str | None = Field(
        None, max_length=100, description="Create a fresh private context with this name"
    )
    llm_config: dict[str, Any] | None = Field(None, description="BYO LLM bundle; Fernet-encrypted")
    channel_ids: list[Any] | None = Field(None, description="Ingest channel selection")
    locale: str | None = Field(None, max_length=10)
    external_team_id: str | None = Field(
        None, max_length=255, description="Platform team id (worker dispatch key)"
    )


class WorkspaceConnectorCreateResponse(BaseModel):
    """Connector setup response. The token + KMC key are shown exactly once."""

    connector_id: UUID
    connector_type: str
    resource_id: str
    resource_pk: UUID
    context_id: UUID | None = None
    token_id: int
    token: str = Field(..., description="Plaintext resource token; save immediately")
    kmc_api_key: str | None = Field(
        None, description="Plaintext KMC write key; shown once (registration flow only)"
    )
    quota_events_per_hour: int
    idempotency_key_prefix: str


class WorkspaceConnectorSummary(TZAwareBaseModel):
    """One connector row for the workspace list view."""

    connector_id: UUID
    connector_type: str
    resource_pk: UUID
    context_id: UUID | None = None
    config_version: int
    created_at: datetime
    created_by: str | None = None


@router.post(
    "", response_model=WorkspaceConnectorCreateResponse, status_code=status.HTTP_201_CREATED
)
async def create_workspace_connector(
    request: WorkspaceConnectorCreateRequest,
    admin: WorkspaceAdmin,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceConnectorCreateResponse:
    """Provision a connector using the Resource Foundation.

    Unlike ``setup_resource``, this endpoint is gated by ``max_connectors`` and
    not by the public-context/resource-token plan gate.
    """
    user_id = admin["user_id"]
    workspace_id = admin.get("current_workspace_id")
    if workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No workspace selected. Please select a workspace first.",
        )

    # #866: validate pii_guardrail_config against the documented schema before it is
    # stored opaquely. Fail-secure — a malformed config is rejected, not silently kept.
    try:
        pii_guardrail_config = validate_pii_guardrail_config(request.pii_guardrail_config)
    except ValueError as ve:
        raise ValidationError(str(ve), field="pii_guardrail_config") from ve

    try:
        result = await ConnectorProvisioningService(db).provision_connector(
            workspace_id=workspace_id,
            user_id=user_id,
            connector_type=request.connector_type,
            resource_id=request.resource_id,
            display_name=request.display_name,
            oauth_tokens=request.oauth_tokens,
            pii_guardrail_config=pii_guardrail_config,
            litellm_virtual_key_id=request.litellm_virtual_key_id,
            virtual_key_valid_until=request.virtual_key_valid_until,
            quota_events_per_hour=request.quota_events_per_hour,
            context_id=request.context_id,
            auto_create_context_name=request.auto_create_context_name,
            llm_config=request.llm_config,
            channel_ids=request.channel_ids,
            locale=request.locale,
            external_team_id=request.external_team_id,
        )
        await db.commit()
        await db.refresh(result.connector)
        await db.refresh(result.token)
    except MemoryCloudException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.error(
            "workspace_connector_create_failed",
            user_id=user_id,
            workspace_id=str(workspace_id),
            connector_type=request.connector_type,
            resource_id=request.resource_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create workspace connector",
        ) from exc

    return WorkspaceConnectorCreateResponse(
        connector_id=result.connector.id,
        connector_type=result.connector.connector_type,
        resource_id=result.resource_id,
        resource_pk=result.resource_pk,
        context_id=result.context_id,
        token_id=result.token.id,
        token=result.plaintext_token,
        kmc_api_key=result.plaintext_kmc_api_key,
        quota_events_per_hour=result.token.quota_events_per_hour,
        idempotency_key_prefix=f"{result.connector.id}:",
    )


@router.get("", response_model=list[WorkspaceConnectorSummary])
async def list_workspace_connectors(
    admin: WorkspaceAdmin,
    db: AsyncSession = Depends(get_db),
) -> list[WorkspaceConnectorSummary]:
    """List connectors for the current workspace (workspace-admin scoped)."""
    workspace_id = admin.get("current_workspace_id")
    if workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No workspace selected. Please select a workspace first.",
        )
    connectors = await ConnectorProvisioningService(db).list_connectors(workspace_id)
    return [
        WorkspaceConnectorSummary(
            connector_id=c.id,
            connector_type=c.connector_type,
            resource_pk=c.resource_pk,
            context_id=c.context_id,
            config_version=c.config_version,
            created_at=c.created_at,
            created_by=c.created_by,
        )
        for c in connectors
    ]


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace_connector(
    connector_id: UUID,
    admin: WorkspaceAdmin,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a connector (workspace-admin scoped).

    Revokes the connector's KMC write key and drops its resource so the worker
    stops on its next config fetch. 404 if the connector is not in the workspace.
    """
    workspace_id = admin.get("current_workspace_id")
    if workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No workspace selected. Please select a workspace first.",
        )
    try:
        deleted = await ConnectorProvisioningService(db).delete_connector(
            workspace_id, connector_id
        )
        if not deleted:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found"
            )
        await db.commit()
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        logger.error(
            "workspace_connector_delete_failed",
            connector_id=str(connector_id),
            workspace_id=str(workspace_id),
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete workspace connector",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
