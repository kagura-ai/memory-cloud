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
    slack_install_handle: str | None = Field(
        None,
        max_length=255,
        description="One-time handle from the Slack OAuth callback; resolved "
        "server-side to oauth_tokens + external_team_id (bot token never sent by client)",
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


class RotateKmcKeyResponse(TZAwareBaseModel):
    """Response after rotating a connector's KMC write key. Shown exactly once."""

    connector_id: UUID
    kmc_api_key: str = Field(..., description="New plaintext KMC write key; save immediately")
    kmc_api_key_expires_at: datetime
    config_version: int


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

    # Resolve a Slack OAuth install handle server-side so the bot token never
    # has to be sent by the browser (Spec 2026-06-02, Plan 4).
    # Mutual exclusion: sending both slack_install_handle AND oauth_tokens is
    # ambiguous — reject it explicitly rather than silently dropping one.
    if request.slack_install_handle and request.oauth_tokens:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either slack_install_handle or oauth_tokens, not both",
        )

    oauth_tokens = request.oauth_tokens
    external_team_id = request.external_team_id
    if request.slack_install_handle:
        # Peek (do NOT consume) so a provisioning failure leaves the handle
        # intact for retry — it is discarded only after a successful create.
        from api.routes.connectors_slack import discard_slack_install, peek_slack_install

        install = await peek_slack_install(request.slack_install_handle)
        if install is None or str(install.get("workspace_id")) != str(workspace_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Slack install handle is invalid or expired",
            )
        oauth_tokens = {
            "bot_token": install.get("bot_token"),
            "installing_admin_user_id": install.get("installing_admin_user_id"),
        }
        external_team_id = install.get("team_id")

    try:
        result = await ConnectorProvisioningService(db).provision_connector(
            workspace_id=workspace_id,
            user_id=user_id,
            connector_type=request.connector_type,
            resource_id=request.resource_id,
            display_name=request.display_name,
            oauth_tokens=oauth_tokens,
            pii_guardrail_config=pii_guardrail_config,
            litellm_virtual_key_id=request.litellm_virtual_key_id,
            virtual_key_valid_until=request.virtual_key_valid_until,
            quota_events_per_hour=request.quota_events_per_hour,
            context_id=request.context_id,
            auto_create_context_name=request.auto_create_context_name,
            llm_config=request.llm_config,
            channel_ids=request.channel_ids,
            locale=request.locale,
            external_team_id=external_team_id,
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

    # Discard the Slack install handle AFTER the commit succeeds and OUTSIDE
    # the try block so a Redis failure here does not trigger a spurious rollback
    # of an already-committed connector.  discard_slack_install is best-effort
    # (handles its own exceptions) — the handle will expire via TTL regardless.
    if request.slack_install_handle:
        await discard_slack_install(request.slack_install_handle)

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
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found")
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


@router.post(
    "/{connector_id}/rotate-kmc-key",
    response_model=RotateKmcKeyResponse,
    status_code=status.HTTP_200_OK,
)
async def rotate_connector_kmc_key(
    connector_id: UUID,
    admin: WorkspaceAdmin,
    db: AsyncSession = Depends(get_db),
) -> RotateKmcKeyResponse:
    """Rotate the KMC write key for a connector (workspace-admin scoped).

    Revokes the current key immediately, mints a replacement with a 365-day
    expiry, and bumps ``config_version`` so the worker re-fetches on its next
    poll. The new plaintext key is returned exactly once.

    **No grace period (v1)**: coordinate rotation during a maintenance window
    or a brief pause in ai-worker activity; the old key is invalid as soon as
    this call returns. A grace-period dual-key approach is deferred to a
    follow-up issue once usage patterns are better understood.
    """
    from utils.exceptions import NotFoundException, ValidationError as SvcValidationError

    workspace_id = admin.get("current_workspace_id")
    if workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No workspace selected. Please select a workspace first.",
        )
    user_id = admin["user_id"]
    try:
        plaintext_key = await ConnectorProvisioningService(db).rotate_kmc_key(
            workspace_id=workspace_id,
            connector_id=connector_id,
            user_id=user_id,
        )
        # Re-fetch to return the updated connector fields.
        from sqlalchemy import select as sa_select

        from models.resource import WorkspaceConnector

        result = await db.execute(
            sa_select(WorkspaceConnector).where(WorkspaceConnector.id == connector_id)
        )
        connector = result.scalar_one()
        await db.commit()
    except NotFoundException:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found")
    except SvcValidationError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        await db.rollback()
        logger.error(
            "workspace_connector_rotate_kmc_key_failed",
            connector_id=str(connector_id),
            workspace_id=str(workspace_id),
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to rotate KMC write key",
        ) from exc
    return RotateKmcKeyResponse(
        connector_id=connector_id,
        kmc_api_key=plaintext_key,
        kmc_api_key_expires_at=connector.kmc_api_key_expires_at,
        config_version=connector.config_version,
    )
