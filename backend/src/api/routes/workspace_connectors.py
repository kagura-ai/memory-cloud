"""Workspace connector setup API routes (Issue #851, F6-b of #755)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from auth.dependencies import WorkspaceAdmin
from db.base import get_db
from db.redis import get_cache, get_redis_client
from models.api_base import TZAwareBaseModel
from models.schemas import validate_pii_guardrail_config
from models.worker_runtime import WorkerRuntimeConfig
from services.connector_provisioning import (
    ConnectorProvisioningService,
    ConnectorRuntimeUpdateResult,
)
from services.security_notification_service import (
    SecurityEvent,
    schedule_security_notification,
)
from services.slack_channels import (
    PRIVATE_CHANNELS_SCOPE,
    SLACK_CHANNEL_TYPES_PUBLIC,
    SlackChannel,
    SlackChannelsPage,
    SlackRateLimited,
    fetch_slack_channels,
)
from utils.exceptions import (
    BadRequestError,
    ConflictError,
    ConnectorScopeError,
    ConnectorTeamConnectedHereError,
    InternalError,
    MemoryCloudException,
    NotFoundException,
    ValidationError,
)
from utils.logger import get_logger
from utils.public_id import PublicIdPrefix, public_id_pattern

logger = get_logger(__name__)

router = APIRouter(prefix="/workspace-connectors", tags=["workspace-connectors"])

# #1391: short server-side cache per connector+cursor for the Slack channel
# picker. conversations.list is Slack rate-limit Tier 2 (~20 req/min per token),
# so an admin repeatedly opening the settings dialog must not burn the budget
# the ai-worker may also need. Keyed by cursor (NOT ``q``) so the client-side
# ``q`` filter reuses the same cached page.
_CHANNELS_CACHE_TTL_SECONDS = 60
# Defensive caps on caller-supplied query params (Slack cursors are opaque
# base64; the picker search box is short).
_CHANNELS_CURSOR_MAX_LEN = 1024
_CHANNELS_QUERY_MAX_LEN = 100


def _channels_cache_prefix(connector_id: UUID | str) -> str:
    """Key prefix of a connector's cached channel pages (the cursor follows)."""
    return f"slack_channels:{connector_id}:"


def _channels_types_key(connector_id: UUID | str) -> str:
    """Key of a connector's public-only listing marker (#1778)."""
    return f"slack_channels_types:{connector_id}"


async def _invalidate_channels_cache(connector_id: str) -> None:
    """Drop a connector's cached channel pages and public-only marker (best-effort).

    The pages are keyed by an opaque Slack cursor, hence the SCAN. The prefix
    ends with ``:`` so it cannot match another connector's keys or the marker.
    """
    try:
        client = get_redis_client()
        keys = [_channels_types_key(connector_id)]
        async for key in client.scan_iter(match=f"{_channels_cache_prefix(connector_id)}*"):
            keys.append(key)
        await client.delete(*keys)
    except Exception:
        logger.warning("slack_channels_cache_invalidate_failed", connector_id=connector_id)


async def _refresh_connected_slack_install(
    db: AsyncSession,
    *,
    workspace_id: UUID,
    connector_id: str,
    bot_token: str,
    install_handle: str,
) -> None:
    """Hand a refused Slack install's bot token to the existing connector (#1880).

    Runs after the create was refused because this workspace already has a
    connector for the Slack workspace. The caller has validated the install
    handle against ``workspace_id`` and the request's ``app_key``, and the
    conflict was found by the install's own team id, so the token belongs to
    the same Slack app and workspace as ``connector_id``.

    Best-effort: the caller re-raises the 409 whatever happens here. On
    success the connector's cached channel listing is dropped and the
    one-time handle is spent; on failure the handle is kept so the admin can
    retry within its TTL.
    """
    from api.routes.connectors_slack import discard_slack_install

    try:
        stored = await ConnectorProvisioningService(db).refresh_slack_bot_token(
            workspace_id, UUID(connector_id), bot_token
        )
        if not stored:
            await db.rollback()
            return
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.warning(
            "workspace_connector_slack_token_refresh_failed",
            connector_id=connector_id,
            workspace_id=str(workspace_id),
            error_type=type(exc).__name__,
        )
        return

    await _invalidate_channels_cache(connector_id)
    await discard_slack_install(install_handle)


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
    locale: str | None = Field(
        None,
        max_length=10,
        description=(
            "Worker pre-compile locale. Must map to the worker Locale "
            "contract ('en' | 'ja'); common BCP-47 forms are normalized "
            "(ja-JP → ja). See models.worker_runtime.WORKER_LOCALES (#1377)."
        ),
    )
    external_team_id: str | None = Field(
        None, max_length=255, description="Platform team id (worker dispatch key)"
    )
    app_key: str = Field(
        "default",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
        description="Stable platform app identity selector",
    )
    slack_install_handle: str | None = Field(
        None,
        max_length=255,
        description="One-time handle from the Slack OAuth callback; resolved "
        "server-side to oauth_tokens + external_team_id (bot token never sent by client)",
    )
    runtime: WorkerRuntimeConfig | None = Field(
        None,
        description="Non-secret per-connector worker controls; omitted uses worker defaults",
    )


class WorkspaceConnectorCreateResponse(BaseModel):
    """Connector setup response. The token + KMC key are shown exactly once."""

    connector_id: UUID
    connector_type: str
    app_key: str
    resource_id: str
    # resource_pk (internal resources.id DB PK) intentionally not exposed (#991):
    # the public `resource_id` slug above is the stable identifier; the internal
    # PK was redundant on this response and is dropped before the 1.0 freeze.
    context_id: UUID | None = None
    token_id: str = Field(
        ...,
        description="Public id of the connector's resource token (`rtok_` + 22 base62 characters)",
        pattern=public_id_pattern(PublicIdPrefix.RESOURCE_TOKEN),
    )
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
    app_key: str
    # Public `resource_id` slug, not the internal `resource_pk` DB key (#991):
    # the slug is the stable surface identifier; the internal PK was an
    # information leak and is dropped before the 1.0 freeze. Resolved via a
    # JOIN in ConnectorProvisioningService.list_connectors.
    resource_id: str
    context_id: UUID | None = None
    config_version: int
    created_at: datetime
    created_by: str | None = None
    runtime: WorkerRuntimeConfig = Field(default_factory=WorkerRuntimeConfig)
    # #1376: vend-settings presence indicators for the admin card. The LLM
    # bundle itself is write-only — only the flag is listed.
    channel_ids: list[Any] | None = None
    locale: str | None = None
    litellm_virtual_key_id: str | None = None
    llm_config_present: bool = False
    # #1389: human-readable identity for the list row — the joined resource's
    # label, the platform team id, and the write-target context's name. All
    # nullable/additive so existing consumers are unaffected.
    display_name: str | None = None
    external_team_id: str | None = None
    context_name: str | None = None
    # #1449: ingest outcome for the row. The 2026-07-21..27 outage was invisible
    # from every screen — the worker's /health said "ok", the connector listed
    # as normal, Slack's webhook answered 200 — while the write-target context
    # had not been touched since 2026-07-19. That date was in this database the
    # whole time. Deliberately reported as fact, never coloured as an error: a
    # low-traffic tenant with no recent writes is healthy, and a row that is
    # always red gets ignored exactly like an alert that always fires.
    last_memory_at: datetime | None = None
    memories_last_7d: int = 0
    # Another connector writes to the same context, so the two figures above
    # are the pair's combined traffic, not this connector's (#1449 review).
    # Surfaced rather than silently averaged away: a shared context is the one
    # case where a dead connector could read as healthy off its sibling.
    ingest_context_shared: bool = False


class WorkspaceConnectorSettingsUpdateRequest(BaseModel):
    """PATCH body for connector vend settings (#1376).

    True PATCH semantics: fields absent from the request are untouched; an
    explicit ``null`` clears. The route forwards ``exclude_unset`` fields to
    the service sentinel, so every default below is just the "absent" marker.
    ``extra="forbid"`` because a silently-dropped typo'd field name would
    read as a successful partial update (#1376 review).
    """

    model_config = ConfigDict(extra="forbid")

    channel_ids: list[str] | None = Field(
        None,
        description="Ingest channel selection; non-empty list of channel ids, or null to clear",
    )
    litellm_virtual_key_id: str | None = Field(None, max_length=255)
    llm_config: dict[str, Any] | None = Field(
        None,
        description="Write-only BYO LLM bundle; stored Fernet-encrypted, "
        "never returned (a presence flag is). Null clears.",
    )
    locale: str | None = Field(
        None,
        max_length=10,
        description="Worker pre-compile locale. Must map to the worker Locale "
        "contract ('en' | 'ja'); common BCP-47 forms are normalized "
        "(ja-JP → ja). Null clears (#1377).",
    )
    context_id: UUID | None = Field(
        None,
        description="Re-point the write-target context (#1428). Must be a live "
        "context in this workspace; null clears the binding (no write target).",
    )
    expected_config_version: int | None = Field(default=None, ge=0)


class WorkspaceConnectorSettingsUpdateResponse(BaseModel):
    """Post-update settings. The LLM bundle itself is never echoed."""

    connector_id: UUID
    channel_ids: list[Any] | None
    litellm_virtual_key_id: str | None
    llm_config_present: bool
    locale: str | None
    config_version: int
    context_id: UUID | None


class WorkspaceConnectorRuntimeUpdateRequest(BaseModel):
    """Complete normalized replacement for tenant-owned worker controls.

    ``runtime`` is required but nullable: an explicit ``null`` clears the
    stored block back to NULL (worker built-in defaults) so a tuned
    connector is not a one-way door. ``expected_config_version`` is the
    optimistic-concurrency guard for the full-document replacement: pass the
    version your snapshot came from and a concurrent change turns into a 409
    instead of a silent revert.
    """

    runtime: WorkerRuntimeConfig | None = Field(...)
    expected_config_version: int | None = Field(default=None, ge=0)


class WorkspaceConnectorRuntimeUpdateResponse(BaseModel):
    """Updated effective controls and the new opaque-source connector revision.

    ``runtime`` is the EFFECTIVE config (worker defaults when the stored
    block was cleared); ``stored`` distinguishes a persisted override from
    the cleared/defaults state.
    """

    connector_id: UUID
    runtime: WorkerRuntimeConfig
    stored: bool
    config_version: int


class RotateKmcKeyResponse(TZAwareBaseModel):
    """Response after rotating a connector's KMC write key. Shown exactly once."""

    connector_id: UUID
    kmc_api_key: str = Field(..., description="New plaintext KMC write key; save immediately")
    kmc_api_key_expires_at: datetime
    config_version: int


class AvailableWorkerApp(BaseModel):
    """Non-secret app identity that a workspace admin may bind."""

    platform: str
    app_key: str
    display_name: str


class ConnectorChannel(BaseModel):
    """One selectable channel for the settings picker (#1391).

    Minimized by design: identity (id, name) plus the two flags the picker has
    to render — ``is_private`` and, since #1451, ``is_member``. Everything else
    Slack returns (member counts, topics, purpose, creator, timestamps) is
    dropped and never reaches the browser.
    """

    id: str
    name: str
    is_private: bool
    # #1451: false means Slack will never deliver this channel's messages to the
    # bot, so selecting it ingests nothing — silently. The picker surfaces this
    # so a selection that cannot work is visible at the moment it is made.
    is_member: bool


class ConnectorChannelsResponse(BaseModel):
    """A page of channels plus Slack's opaque forward cursor (#1391)."""

    channels: list[ConnectorChannel]
    next_cursor: str | None = None
    # #1778: scopes this listing lacked. ``["groups:read"]`` means the install
    # predates the private-channel listing and the page is public-only; the
    # picker says so and points at reconnecting Slack. Reported here rather
    # than via the pending-install summary (#1758) because the granted scopes
    # are not persisted on the connector — the listing itself is the only
    # place an existing install reveals its grant.
    missing_scopes: list[str] = Field(default_factory=list)


_CREATE_CONFLICT_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: {
        "description": (
            "`RES-002`. For a platform team that is already connected, "
            "`details.reason` says where (#1753): `connector_team_connected_here` "
            "(this workspace; `details.connector_id` and `details.display_name` "
            "name the existing connector — edit it instead) or "
            "`connector_team_connected_elsewhere` (another workspace; no other "
            "details). A `resource_id` clash is a 409 without `reason`."
        ),
        "content": {
            "application/json": {
                "example": {
                    "error": "RES-002",
                    "message": (
                        "This slack team is already connected to another workspace. "
                        "A team can be connected to one workspace only."
                    ),
                    "details": {"reason": "connector_team_connected_elsewhere"},
                }
            }
        },
    }
}


@router.post(
    "",
    response_model=WorkspaceConnectorCreateResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_CREATE_CONFLICT_RESPONSE,
)
async def create_workspace_connector(
    request: WorkspaceConnectorCreateRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
    admin: WorkspaceAdmin,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceConnectorCreateResponse:
    """Provision a connector using the Resource Foundation.

    Requires a plan with the ``connectors`` feature (XL, #1551); existing
    connectors on lower plans keep working. The ``max_connectors`` seat cap
    applies second, inside ``ConnectorProvisioningService``.
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
        install_app_key = str(install.get("app_key") or "default")
        if request.app_key != install_app_key:
            raise BadRequestError(
                "Slack install handle belongs to a different app identity",
                error_code="CONNECTOR-003",
            )

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
            app_key=request.app_key,
            runtime_config=(
                request.runtime.model_dump(mode="json") if request.runtime is not None else None
            ),
        )
        await db.commit()
        await db.refresh(result.connector)
        await db.refresh(result.token)
    except ConnectorTeamConnectedHereError as conflict:
        await db.rollback()
        # #1880: still a 409 — but an OAuth install that landed on a Slack
        # workspace this workspace already holds may carry a NEW bot token
        # (app removed and reinstalled, token revoked). Keep it, or the
        # existing connector stays on a dead token.
        if (
            request.slack_install_handle
            and request.connector_type == "slack"
            and oauth_tokens
            and oauth_tokens.get("bot_token")
        ):
            await _refresh_connected_slack_install(
                db,
                workspace_id=workspace_id,
                connector_id=str(conflict.details.get("connector_id") or ""),
                bot_token=str(oauth_tokens["bot_token"]),
                install_handle=request.slack_install_handle,
            )
        raise
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

    # #1752: a KMC write key was minted for the registering admin.
    if result.kmc_api_key_name is not None:
        schedule_security_notification(
            background_tasks,
            user_id=user_id,
            event=SecurityEvent.API_KEY_CREATED,
            request=http_request,
            key_name=result.kmc_api_key_name,
        )

    return WorkspaceConnectorCreateResponse(
        connector_id=result.connector.id,
        connector_type=result.connector.connector_type,
        app_key=result.connector.app_key,
        resource_id=result.resource_id,
        context_id=result.context_id,
        token_id=result.token.public_id,
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
    items = await ConnectorProvisioningService(db).list_connectors(workspace_id)
    return [
        WorkspaceConnectorSummary(
            connector_id=item.connector.id,
            connector_type=item.connector.connector_type,
            app_key=item.connector.app_key,
            resource_id=item.resource_id,
            context_id=item.connector.context_id,
            config_version=item.connector.config_version,
            created_at=item.connector.created_at,
            created_by=item.connector.created_by,
            # Lenient rehydrate (#1350 review): one drifted stored document
            # must not 500 the whole workspace list — fall back to defaults.
            runtime=(
                WorkerRuntimeConfig.from_stored(item.connector.runtime_config)
                or WorkerRuntimeConfig()
            ),
            channel_ids=item.connector.channel_ids,
            locale=item.connector.locale,
            litellm_virtual_key_id=item.connector.litellm_virtual_key_id,
            llm_config_present=bool(item.connector.llm_config_encrypted),
            display_name=item.display_name,
            external_team_id=item.connector.external_team_id,
            context_name=item.context_name,
            last_memory_at=item.last_memory_at,
            memories_last_7d=item.memories_last_7d,
            ingest_context_shared=item.ingest_context_shared,
        )
        for item in items
    ]


@router.patch(
    "/{connector_id}/runtime",
    response_model=WorkspaceConnectorRuntimeUpdateResponse,
)
async def update_workspace_connector_runtime(
    connector_id: UUID,
    request: WorkspaceConnectorRuntimeUpdateRequest,
    admin: WorkspaceAdmin,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceConnectorRuntimeUpdateResponse:
    """Replace per-connector runtime controls (workspace-admin scoped)."""
    workspace_id = admin.get("current_workspace_id")
    if workspace_id is None:
        raise BadRequestError(
            "No workspace selected. Please select a workspace first.",
        )
    try:
        update_result: ConnectorRuntimeUpdateResult = await ConnectorProvisioningService(
            db
        ).update_runtime_config(
            workspace_id=workspace_id,
            connector_id=connector_id,
            runtime_config=(
                request.runtime.model_dump(mode="json") if request.runtime is not None else None
            ),
            user_id=admin["user_id"],
            expected_config_version=request.expected_config_version,
        )
        await db.commit()
    except NotFoundException as exc:
        await db.rollback()
        raise NotFoundException("Connector") from exc
    except ConflictError:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.error(
            "workspace_connector_runtime_update_failed",
            connector_id=str(connector_id),
            workspace_id=str(workspace_id),
            user_id=admin["user_id"],
            error_type=type(exc).__name__,
        )
        raise InternalError("Failed to update connector runtime") from exc

    stored = update_result.runtime_config is not None
    return WorkspaceConnectorRuntimeUpdateResponse(
        connector_id=connector_id,
        runtime=(
            WorkerRuntimeConfig.model_validate(update_result.runtime_config)
            if stored
            else WorkerRuntimeConfig()
        ),
        stored=stored,
        config_version=update_result.config_version,
    )


@router.patch(
    "/{connector_id}",
    response_model=WorkspaceConnectorSettingsUpdateResponse,
)
async def update_workspace_connector_settings(
    connector_id: UUID,
    request: WorkspaceConnectorSettingsUpdateRequest,
    admin: WorkspaceAdmin,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceConnectorSettingsUpdateResponse:
    """PATCH connector vend settings (#1376): channel_ids / LLM binding / locale.

    Repairs connectors born un-vendable (the create endpoint was previously
    the only writer of these columns). True PATCH semantics — absent fields
    are untouched, explicit ``null`` clears — with the runtime PATCH's
    ``expected_config_version`` optimistic lock (409 on staleness).
    """
    workspace_id = admin.get("current_workspace_id")
    if workspace_id is None:
        raise BadRequestError(
            "No workspace selected. Please select a workspace first.",
        )
    # exclude_unset distinguishes an explicit null (clear) from an absent
    # field (untouched) and derives the field set from the model itself — a
    # hand-listed tuple here would silently drop a future request field
    # (#1376 review).
    kwargs: dict[str, Any] = request.model_dump(
        exclude_unset=True, exclude={"expected_config_version"}
    )
    try:
        settings_result = await ConnectorProvisioningService(db).update_connector_settings(
            workspace_id=workspace_id,
            connector_id=connector_id,
            user_id=admin["user_id"],
            expected_config_version=request.expected_config_version,
            **kwargs,
        )
        await db.commit()
    except NotFoundException:
        # Re-raise as-is so a bad context_id re-point (#1428) surfaces as
        # "Context not found" rather than being masked as "Connector". Both
        # ids are caller-supplied (URL / body), so this leaks nothing.
        await db.rollback()
        raise
    except (ConflictError, ValidationError):
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.error(
            "workspace_connector_settings_update_failed",
            connector_id=str(connector_id),
            workspace_id=str(workspace_id),
            user_id=admin["user_id"],
            error_type=type(exc).__name__,
        )
        raise InternalError("Failed to update connector settings") from exc

    return WorkspaceConnectorSettingsUpdateResponse(
        connector_id=connector_id,
        channel_ids=settings_result.channel_ids,
        litellm_virtual_key_id=settings_result.litellm_virtual_key_id,
        llm_config_present=settings_result.llm_config_present,
        locale=settings_result.locale,
        config_version=settings_result.config_version,
        context_id=settings_result.context_id,
    )


@router.get("/available-apps", response_model=list[AvailableWorkerApp])
async def list_available_worker_apps(
    admin: WorkspaceAdmin,
    db: AsyncSession = Depends(get_db),
) -> list[AvailableWorkerApp]:
    """List active, non-secret app identities available for connector binding."""
    if admin.get("current_workspace_id") is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No workspace selected. Please select a workspace first.",
        )
    from services.worker_app_identity import WorkerAppIdentityService

    identities = await WorkerAppIdentityService(db).list_identities(active_only=True)
    return [
        AvailableWorkerApp(
            platform=identity.platform,
            app_key=identity.app_key,
            display_name=identity.display_name,
        )
        for identity in identities
    ]


@router.get("/{connector_id}/channels", response_model=ConnectorChannelsResponse)
async def list_connector_channels(
    connector_id: UUID,
    admin: WorkspaceAdmin,
    cursor: Annotated[str | None, Query(max_length=_CHANNELS_CURSOR_MAX_LEN)] = None,
    q: Annotated[str | None, Query(max_length=_CHANNELS_QUERY_MAX_LEN)] = None,
    db: AsyncSession = Depends(get_db),
) -> ConnectorChannelsResponse:
    """List a Slack connector's channels for the settings picker (#1391, #1778).

    Server-side proxy of Slack ``conversations.list`` using the connector's
    Fernet-decrypted bot token — the token never reaches the browser. Read-only
    and workspace-admin scoped; a cross-workspace / unknown connector is a
    uniform 404. The optional ``q`` filters the fetched page by name
    (case-insensitive substring); Slack's API has no name filter.

    Public channels and the private channels the bot is a member of are
    listed (#1778). An install granted before ``groups:read`` was requested
    still gets its public listing, with ``missing_scopes=["groups:read"]`` so
    the picker can explain the gap and suggest reconnecting.

    Errors degrade gracefully to the manual-ID entry lane rather than 5xx:
    a token missing the ``channels:read`` scope (legacy installs) is a 409
    ``CONNECTOR-SCOPE``; Slack rate-limiting is a 429 with ``Retry-After``.
    """
    workspace_id = admin.get("current_workspace_id")
    if workspace_id is None:
        raise BadRequestError("No workspace selected. Please select a workspace first.")

    connector = await ConnectorProvisioningService(db).get_connector(workspace_id, connector_id)
    if connector is None:
        # Cross-workspace and unknown connectors are indistinguishable — no
        # cross-tenant existence oracle (same contract as the settings PATCH).
        # Omit the id (matches the runtime-PATCH 404) for a uniform message.
        raise NotFoundException("Connector")
    if connector.connector_type != "slack":
        # v1 picker is Slack-only; other providers arrive with their own
        # hierarchy shapes (design non-goal). Non-existence-leaking 400.
        raise BadRequestError(
            "Channel listing is only available for Slack connectors.",
            error_code="CONNECTOR-CHANNELS-001",
        )

    tokens = connector.get_oauth_tokens() or {}
    bot_token = tokens.get("bot_token")
    if not bot_token:
        # No bot token at all (never OAuth-installed / cleared). The client
        # fallback is the same manual-entry lane as a missing scope.
        raise ConnectorScopeError(
            "This connector has no Slack bot token; enter channel IDs manually."
        )

    cache_key = f"{_channels_cache_prefix(connector_id)}{cursor or ''}"
    page: SlackChannelsPage | None = None
    cached = await get_cache(cache_key)
    if cached is not None:
        try:
            payload = json.loads(cached)
            page = SlackChannelsPage(
                channels=[SlackChannel(**c) for c in payload["channels"]],
                next_cursor=payload.get("next_cursor"),
                # Required key: a pre-#1778 entry (public-only) must miss.
                private_listing=bool(payload["private_listing"]),
            )
        except (ValueError, KeyError, TypeError):
            # A corrupt / schema-drifted cache entry must never fail the
            # request — treat it as a miss and refetch.
            page = None

    if page is None:
        # #1778: a connector whose last page came from the public-only retry
        # is marked for the cache TTL, so its later pages ask for public
        # channels straight away — one Tier-2 call per page, and a public-only
        # cursor is never replayed into a mixed request.
        # #1880: the marker applies to cursor pages only. Every public-only
        # page re-arms it, so on its own it could outlive the TTL for as long
        # as pages keep loading; the first page therefore always probes the
        # mixed listing, and a reconnect that widened the grant is picked up
        # as soon as the cached first page expires.
        types_key = _channels_types_key(connector_id)
        public_only = (
            cursor is not None and (await get_cache(types_key)) == SLACK_CHANNEL_TYPES_PUBLIC
        )
        try:
            page = await fetch_slack_channels(
                bot_token=bot_token, cursor=cursor, public_only=public_only
            )
        except SlackRateLimited as exc:
            # Surface Slack's Tier-2 rate limit as a 429 with Retry-After
            # passthrough so the frontend shows the manual-entry fallback
            # rather than spinning. HTTPException carries the header through
            # http_exception_handler (the MemoryCloudException handler drops
            # response headers).
            headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Slack rate limit reached; please retry shortly.",
                headers=headers,
            ) from exc

        # Best-effort cache write of the minimized page (no token, no extra
        # metadata). A Redis outage here must not fail an otherwise-good fetch.
        try:
            await get_redis_client().setex(
                cache_key,
                _CHANNELS_CACHE_TTL_SECONDS,
                json.dumps(
                    {
                        "channels": [
                            {
                                "id": c.id,
                                "name": c.name,
                                "is_private": c.is_private,
                                "is_member": c.is_member,
                            }
                            for c in page.channels
                        ],
                        "next_cursor": page.next_cursor,
                        # #1778: cached with the page so a legacy install's
                        # two-request miss (mixed, then public-only) is paid
                        # once per TTL, not on every dialog open.
                        "private_listing": page.private_listing,
                    }
                ),
            )
            if page.private_listing:
                # #1880: the grant covers private channels (again) — stop
                # steering the remaining pages to the public-only listing.
                await get_redis_client().delete(types_key)
            else:
                await get_redis_client().setex(
                    types_key, _CHANNELS_CACHE_TTL_SECONDS, SLACK_CHANNEL_TYPES_PUBLIC
                )
        except Exception:
            logger.warning("slack_channels_cache_write_failed", connector_id=str(connector_id))

    channels = page.channels
    if q:
        needle = q.strip().lower()
        if needle:
            channels = [c for c in channels if needle in c.name.lower()]

    return ConnectorChannelsResponse(
        channels=[
            ConnectorChannel(id=c.id, name=c.name, is_private=c.is_private, is_member=c.is_member)
            for c in channels
        ],
        next_cursor=page.next_cursor,
        missing_scopes=[] if page.private_listing else [PRIVATE_CHANNELS_SCOPE],
    )


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
    http_request: Request,
    background_tasks: BackgroundTasks,
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

    Issue #1752: the key's owner is emailed a security notice after the
    commit, naming the rotating admin when that is someone else.
    """
    from utils.exceptions import NotFoundException
    from utils.exceptions import ValidationError as SvcValidationError

    workspace_id = admin.get("current_workspace_id")
    if workspace_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No workspace selected. Please select a workspace first.",
        )
    user_id = admin["user_id"]
    try:
        rotation = await ConnectorProvisioningService(db).rotate_kmc_key(
            workspace_id=workspace_id,
            connector_id=connector_id,
            user_id=user_id,
        )
        await db.commit()
    except NotFoundException as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found"
        ) from exc
    except SvcValidationError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
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
    schedule_security_notification(
        background_tasks,
        user_id=rotation.key_owner_user_id,
        event=SecurityEvent.API_KEY_REGENERATED,
        request=http_request,
        key_name=rotation.key_name,
        actor_user_id=user_id,
    )
    return RotateKmcKeyResponse(
        connector_id=connector_id,
        kmc_api_key=rotation.plaintext_kmc_api_key,
        kmc_api_key_expires_at=rotation.expires_at,
        config_version=rotation.config_version,
    )
