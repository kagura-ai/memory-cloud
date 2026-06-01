"""ai-worker connector provisioning service (Issue #851, F6-b of #755).

This service is the shared backend path for REST and MCP connector setup. It
reuses the Resource Foundation instead of introducing connector-specific ingest
tables: one ``resources`` row, one ``workspace_connectors`` row, and one
resource token scoped to that resource.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.resource_tokens import ResourceTokenManager
from models.auth import Workspace
from models.resource import ResourceToken, WorkspaceConnector
from services.resource_lookup import resolve_resource_pk, upsert_resource
from utils.exceptions import ConflictError, MemoryCloudException, NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

CONNECTOR_TYPES = frozenset({"slack", "discord", "teams"})
_RESOURCE_ID_RE = re.compile(r"^[a-z0-9_-]+$")


@dataclass(frozen=True)
class ConnectorProvisioningResult:
    """Result returned after provisioning a connector."""

    connector: WorkspaceConnector
    token: ResourceToken
    plaintext_token: str
    resource_id: str
    resource_pk: UUID


class ConnectorProvisioningService:
    """Provision ai-worker chat-ingest connectors atomically.

    The caller owns transaction finalization. The service only flushes staged
    ORM objects so REST and MCP handlers can commit on success or rollback on
    any exception.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def provision_connector(
        self,
        *,
        workspace_id: UUID,
        user_id: str,
        connector_type: str,
        resource_id: str,
        display_name: str | None = None,
        oauth_tokens: dict[str, Any] | None = None,
        pii_guardrail_config: dict[str, Any] | None = None,
        litellm_virtual_key_id: str | None = None,
        virtual_key_valid_until: datetime | None = None,
        quota_events_per_hour: int = 1000,
    ) -> ConnectorProvisioningResult:
        """Create resource + connector + connector-scoped token in one flow."""
        self._validate_inputs(connector_type, resource_id, quota_events_per_hour)

        workspace = await self._get_workspace(workspace_id)
        await self._enforce_connector_seat_cap(workspace, workspace_id)

        existing_resource_pk = await resolve_resource_pk(self.db, workspace_id, resource_id)
        resource_pk = await upsert_resource(
            self.db,
            workspace_id=workspace_id,
            resource_id=resource_id,
            name=display_name or f"{connector_type} connector",
            created_by=user_id,
        )

        existing_connector = await self._get_connector_for_resource(resource_pk)
        if existing_resource_pk is not None and existing_connector is None:
            raise ConflictError(
                (
                    f"Resource '{resource_id}' already exists and is not connector-owned. "
                    "Choose a fresh resource_id for this connector."
                ),
                resource_pk=str(existing_resource_pk),
            )
        if existing_connector is not None:
            raise ConflictError(
                f"Resource '{resource_id}' already has a workspace connector.",
                connector_id=str(existing_connector.id),
            )

        connector = WorkspaceConnector(
            resource_pk=resource_pk,
            workspace_id=workspace_id,
            connector_type=connector_type,
            pii_guardrail_config=pii_guardrail_config,
            litellm_virtual_key_id=litellm_virtual_key_id,
            virtual_key_valid_until=virtual_key_valid_until,
            created_by=user_id,
        )
        connector.set_oauth_tokens(oauth_tokens)
        self.db.add(connector)
        await self.db.flush()

        plaintext_token, token_record = await ResourceTokenManager(self.db).create_token(
            resource_id=resource_id,
            resource_pk=resource_pk,
            workspace_id=workspace_id,
            description=f"Connector token for {connector_type}:{connector.id}",
            quota_events_per_hour=quota_events_per_hour,
            created_by=user_id,
        )

        logger.info(
            "workspace_connector_provisioned",
            connector_id=str(connector.id),
            connector_type=connector_type,
            resource_id=resource_id,
            resource_pk=str(resource_pk),
            workspace_id=str(workspace_id),
            user_id=user_id,
        )

        return ConnectorProvisioningResult(
            connector=connector,
            token=token_record,
            plaintext_token=plaintext_token,
            resource_id=resource_id,
            resource_pk=resource_pk,
        )

    @staticmethod
    def _validate_inputs(
        connector_type: str,
        resource_id: str,
        quota_events_per_hour: int,
    ) -> None:
        if connector_type not in CONNECTOR_TYPES:
            raise ValidationError(
                "connector_type must be one of: slack, discord, teams",
                field="connector_type",
            )
        if not resource_id or len(resource_id) > 255 or not _RESOURCE_ID_RE.match(resource_id):
            raise ValidationError(
                "resource_id must be lowercase alphanumeric with hyphen/underscore only",
                field="resource_id",
            )
        if quota_events_per_hour < 1 or quota_events_per_hour > 10000:
            raise ValidationError(
                "quota_events_per_hour must be between 1 and 10000",
                field="quota_events_per_hour",
            )

    async def _get_workspace(self, workspace_id: UUID) -> Workspace:
        result = await self.db.execute(select(Workspace).where(Workspace.id == workspace_id))
        workspace = result.scalar_one_or_none()
        if workspace is None:
            raise NotFoundException("Workspace", str(workspace_id))
        return workspace

    async def _enforce_connector_seat_cap(self, workspace: Workspace, workspace_id: UUID) -> None:
        # Issue #857: acquire a per-workspace ``pg_advisory_xact_lock`` before
        # the count read so two concurrent provisions for the last seat cannot
        # both observe ``count < cap`` and both insert (TOCTOU over-provision).
        # The lock is xact-scoped, so the caller's transaction — which extends
        # from this gate through ``db.flush()`` of the new connector — keeps the
        # serialization across the read-then-write. Mirrors the workspace-create
        # cap (#677, ``quota_service.py``) and the admin-bonus path (``admin.py``).
        await self._acquire_connector_seat_lock(workspace_id)

        max_connectors = workspace.effective_max_connectors
        active_count_result = await self.db.execute(
            select(func.count(WorkspaceConnector.id)).where(
                WorkspaceConnector.workspace_id == workspace_id
            )
        )
        active_count = active_count_result.scalar() or 0
        if active_count >= max_connectors:
            raise MemoryCloudException(
                (f"Connector seat limit reached. Your plan allows {max_connectors} connector(s)."),
                status_code=403,
                error_code="CONNECTOR-001",
                max_connectors=max_connectors,
                active_connectors=active_count,
            )

    async def _acquire_connector_seat_lock(self, workspace_id: UUID) -> None:
        """Take the per-workspace advisory lock guarding seat-cap enforcement.

        Unlike the workspace-creation cap (#677), connector seat-cap is always
        enforced — there is no log-only rollout flag — so the lock-error policy
        is unconditionally **fail-closed**: a lock-acquire failure denies the
        provision rather than letting it through.

        ``SET LOCAL lock_timeout = '5s'`` bounds the wait so a pathologically
        long peer transaction cannot stall the worker indefinitely. On SQLSTATE
        ``55P03`` (``lock_not_available``) we rollback the poisoned session and
        raise a retriable 503; any other DB error propagates after rollback. The
        reset to ``'0'`` after a successful acquire keeps the 5s timeout from
        bleeding into the subsequent count SELECT, connector INSERT, and token
        mint that share this transaction.

        ``hashtextextended(:key, 0)`` returns the 64-bit hash matching the
        bigint signature of single-key ``pg_advisory_xact_lock``; 32-bit
        ``hashtext`` would collide at ~65k workspaces and block unrelated
        tenants (PR #686 loop 4).
        """
        lock_key = f"connector_seat:{workspace_id}"
        await self.db.execute(text("SET LOCAL lock_timeout = '5s'"))
        try:
            await self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))").bindparams(
                    key=lock_key
                )
            )
        except DBAPIError as exc:
            sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            # The session is poisoned until rollback — issue it before raising
            # so the next request on this session starts clean.
            await self.db.rollback()
            if sqlstate == "55P03":
                logger.warning(
                    "connector_seat_lock_timeout",
                    workspace_id=str(workspace_id),
                )
                raise MemoryCloudException(
                    "Connector seat lock unavailable; please retry.",
                    status_code=503,
                    error_code="CONNECTOR-002",
                ) from exc
            raise
        # Reset is on the success path only; on failure the rollback above has
        # already cleared the SET LOCAL along with the rest of the transaction.
        await self.db.execute(text("SET LOCAL lock_timeout = '0'"))

    async def _get_connector_for_resource(
        self,
        resource_pk: UUID,
    ) -> WorkspaceConnector | None:
        result = await self.db.execute(
            select(WorkspaceConnector).where(WorkspaceConnector.resource_pk == resource_pk)
        )
        return result.scalar_one_or_none()


async def get_connector_id_for_resource_pk(
    db: AsyncSession,
    resource_pk: UUID | None,
) -> UUID | None:
    """Return the connector ID for a connector-owned resource, if any."""
    if resource_pk is None:
        return None
    result = await db.execute(
        select(WorkspaceConnector.id).where(WorkspaceConnector.resource_pk == resource_pk)
    )
    return result.scalar_one_or_none()


def validate_connector_idempotency_key(
    *,
    connector_id: UUID | None,
    idempotency_key: str | None,
) -> None:
    """Enforce the ``{connector_id}:{summary_id}`` idempotency contract.

    Non-connector resources are unchanged. Connector-owned resources must send
    an idempotency key prefixed by their connector UUID so the globally unique
    ``resource_events.idempotency_key`` column cannot collide across connector
    namespaces.
    """
    if connector_id is None:
        return

    expected_prefix = f"{connector_id}:"
    if not idempotency_key or not idempotency_key.startswith(expected_prefix):
        raise ValidationError(
            f"Connector events must use idempotency_key prefix '{expected_prefix}'.",
            field="idempotency_key",
            connector_id=str(connector_id),
        )
