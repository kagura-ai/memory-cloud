"""Workspace connector setup API routes (Issue #851, F6-b of #755)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceAdmin
from db.base import get_db
from models.schemas import validate_pii_guardrail_config
from services.connector_provisioning import ConnectorProvisioningService
from utils.exceptions import MemoryCloudException
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


class WorkspaceConnectorCreateResponse(BaseModel):
    """Connector setup response. The token is shown exactly once."""

    connector_id: UUID
    connector_type: str
    resource_id: str
    resource_pk: UUID
    token_id: int
    token: str = Field(..., description="Plaintext resource token; save immediately")
    quota_events_per_hour: int
    idempotency_key_prefix: str


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
        raise MemoryCloudException(
            str(ve), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, error_code="VALIDATION-001"
        ) from ve

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
        token_id=result.token.id,
        token=result.plaintext_token,
        quota_events_per_hour=result.token.quota_events_per_hour,
        idempotency_key_prefix=f"{result.connector.id}:",
    )
