"""ai-worker service endpoints (Spec 2026-06-02, Plan 3).

The kagura-memory-ai-worker is a Kagura-operated shared multi-tenant worker
(Model B). It dispatches inbound platform events (e.g. Slack) by team id, then
fetches the per-connector config from here — replacing the static
``connector.json`` file. Authenticated by a dedicated worker service token
(NOT a user session or workspace API key), and intended to be reachable only
over the internal network (not exposed publicly via Caddy).

The response intentionally carries secrets (Slack bot token, BYO LLM key, the
workspace-scoped KMC write key) decrypted at read time — callers MUST treat the
payload as sensitive and never log it.
"""

from __future__ import annotations

import secrets
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.base import get_db
from models.api_base import TZAwareBaseModel
from services.connector_provisioning import ConnectorProvisioningService
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/workers", tags=["workers"])


async def verify_worker_token(authorization: str | None = Header(None)) -> None:
    """Authenticate the ai-worker by its shared service token (RFC 6750 Bearer).

    Fail-closed: an unset ``WORKER_SERVICE_TOKEN`` disables the endpoint (503),
    so a misconfigured deployment never serves connector secrets unauthenticated.
    """
    expected = get_settings().worker_service_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Worker config endpoint is not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Worker service token required",
        )
    token = authorization[len("Bearer ") :]
    if not secrets.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid worker service token",
        )


class WorkerConnectorConfig(TZAwareBaseModel):
    """Per-connector config handed to the ai-worker. Contains secrets."""

    connector_id: UUID
    workspace_id: UUID
    context_id: UUID
    platform: str
    locale: str | None = None
    slack: dict[str, Any]
    kmc: dict[str, Any]
    llm: dict[str, Any] | None = None
    pii_guardrail_config: dict[str, Any] | None = None


@router.get("/config", response_model=WorkerConnectorConfig)
async def get_worker_config(
    platform: Literal["slack", "discord", "teams"],
    team_id: str = Query(..., max_length=255),
    _: None = Depends(verify_worker_token),
    db: AsyncSession = Depends(get_db),
) -> WorkerConnectorConfig:
    """Return the connector config for a platform team (worker dispatch).

    404 when no connector serves the team, or the connector has no write-target
    context yet (registration incomplete) — the worker treats both as not-ready.
    """
    connector = await ConnectorProvisioningService(db).get_connector_for_dispatch(
        connector_type=platform, external_team_id=team_id
    )
    kmc_api_key = connector.get_kmc_api_key() if connector else None
    if connector is None or connector.context_id is None or not kmc_api_key:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No ready connector for this team",
        )

    oauth = connector.get_oauth_tokens() or {}
    slack = {
        "bot_token": oauth.get("bot_token"),
        "team_id": connector.external_team_id,
        "installing_admin_user_id": oauth.get("installing_admin_user_id"),
        "channel_ids": connector.channel_ids or [],
    }
    return WorkerConnectorConfig(
        connector_id=connector.id,
        workspace_id=connector.workspace_id,
        context_id=connector.context_id,
        platform=connector.connector_type,
        locale=connector.locale,
        slack=slack,
        kmc={"mcp_url": get_settings().kmc_mcp_url, "api_key": kmc_api_key},
        llm=connector.get_llm_config(),
        pii_guardrail_config=connector.pii_guardrail_config,
    )
