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

from sqlalchemy import delete, func, select, text, update
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
    # Spec 2026-06-02 (registration flow). Both NULL on the legacy path that
    # does not pass context info; populated when the connector is registered
    # with a write-target context.
    context_id: UUID | None = None
    plaintext_kmc_api_key: str | None = None


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
        context_id: UUID | None = None,
        auto_create_context_name: str | None = None,
        llm_config: dict[str, Any] | None = None,
        channel_ids: list[Any] | None = None,
        locale: str | None = None,
        external_team_id: str | None = None,
    ) -> ConnectorProvisioningResult:
        """Create resource + connector + connector-scoped token in one flow.

        Spec 2026-06-02 (registration flow): when ``context_id`` or
        ``auto_create_context_name`` is supplied, the connector is bound to a
        write-target context and a workspace-scoped KMC write key is minted and
        stored Fernet-encrypted so the worker config endpoint can hand it back.
        Callers that pass neither keep the legacy behaviour (no context, no
        KMC key) for backward compatibility.
        """
        self._validate_inputs(connector_type, resource_id, quota_events_per_hour)

        workspace = await self._get_workspace(workspace_id)

        # ``auto_create_context_name`` goes through ContextService.create_context,
        # which COMMITS mid-flow — that would release the seat-cap advisory lock
        # before the connector insert and re-open the TOCTOU over-provision hole.
        # So for the auto-create path: non-locking pre-check → create+commit the
        # context → THEN take the lock for the authoritative gate (held through
        # the connector flush + the caller's commit). The legacy / existing-
        # context paths keep the original order (lock first) since they never
        # commit before the insert.
        if auto_create_context_name:
            await self._check_seat_cap_available(workspace, workspace_id)
            resolved_context_id = await self._resolve_context(
                workspace=workspace,
                user_id=user_id,
                context_id=None,
                auto_create_context_name=auto_create_context_name,
            )
            await self._enforce_connector_seat_cap(workspace, workspace_id)
        else:
            await self._enforce_connector_seat_cap(workspace, workspace_id)
            resolved_context_id = await self._resolve_context(
                workspace=workspace,
                user_id=user_id,
                context_id=context_id,
                auto_create_context_name=None,
            )

        # One platform team maps to exactly one connector — reject a duplicate
        # (connector_type, external_team_id) with a clean 409 instead of letting
        # the UNIQUE index raise an opaque IntegrityError. Guards against a
        # tenant hijacking another tenant's Slack team dispatch (code-review).
        if external_team_id is not None:
            existing_team = await self.get_connector_for_dispatch(connector_type, external_team_id)
            if existing_team is not None:
                raise ConflictError(
                    f"A {connector_type} connector for team '{external_team_id}' already exists.",
                    connector_id=str(existing_team.id),
                )

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
            context_id=resolved_context_id,
            locale=locale,
            channel_ids=channel_ids,
            external_team_id=external_team_id,
            pii_guardrail_config=pii_guardrail_config,
            litellm_virtual_key_id=litellm_virtual_key_id,
            virtual_key_valid_until=virtual_key_valid_until,
            created_by=user_id,
        )
        connector.set_oauth_tokens(oauth_tokens)
        connector.set_llm_config(llm_config)
        self.db.add(connector)
        await self.db.flush()

        # Mint the workspace-scoped KMC write key (path a) when this connector
        # has a write-target context. Stored Fernet-encrypted on the connector
        # so the worker config endpoint can return it on every fetch.
        plaintext_kmc_api_key: str | None = None
        if resolved_context_id is not None:
            from auth.api_keys import APIKeyManager

            plaintext_kmc_api_key, _ = await APIKeyManager(self.db).create_key(
                name=f"connector:{connector.id}",
                user_id=user_id,
                workspace_id=workspace_id,
            )
            connector.set_kmc_api_key(plaintext_kmc_api_key)
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
            context_id=resolved_context_id,
            plaintext_kmc_api_key=plaintext_kmc_api_key,
        )

    async def _resolve_context(
        self,
        *,
        workspace: Workspace,
        user_id: str,
        context_id: UUID | None,
        auto_create_context_name: str | None,
    ) -> UUID | None:
        """Resolve the write-target context for a connector.

        ``context_id`` selects an existing workspace context (verified to belong
        to the workspace); ``auto_create_context_name`` creates a fresh private
        context. Returns ``None`` when neither is given (legacy path).

        Auto-created contexts are attributed to the workspace owner
        (``created_by=workspace.owner_user_id``) so the private-context
        owner-only rule in ``ContextService`` is satisfied even when the acting
        principal is a workspace admin (the endpoint admits both).
        """
        if context_id is not None and auto_create_context_name:
            raise ValidationError(
                "Provide either context_id or auto_create_context_name, not both.",
                field="context_id",
            )
        if context_id is not None:
            from models.auth import Context

            result = await self.db.execute(
                select(Context).where(
                    Context.id == context_id,
                    Context.workspace_id == workspace.id,
                )
            )
            if result.scalar_one_or_none() is None:
                raise NotFoundException("Context", str(context_id))
            return context_id
        if auto_create_context_name:
            from services.context_service import ContextService

            context = await ContextService(self.db).create_context(
                workspace_id=workspace.id,
                name=auto_create_context_name,
                created_by=workspace.owner_user_id,
            )
            return context.id
        return None

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

    @staticmethod
    def _raise_seat_cap(max_connectors: int, active_connectors: int) -> None:
        raise MemoryCloudException(
            (f"Connector seat limit reached. Your plan allows {max_connectors} connector(s)."),
            status_code=403,
            error_code="CONNECTOR-001",
            max_connectors=max_connectors,
            active_connectors=active_connectors,
        )

    async def _count_active_connectors(self, workspace_id: UUID) -> int:
        result = await self.db.execute(
            select(func.count(WorkspaceConnector.id)).where(
                WorkspaceConnector.workspace_id == workspace_id
            )
        )
        return result.scalar() or 0

    async def _check_seat_cap_available(self, workspace: Workspace, workspace_id: UUID) -> None:
        """Non-locking seat-cap pre-check.

        Used on the auto-create-context path BEFORE creating (committing) a
        context, so a plainly-full cap is rejected without leaving an orphan
        context behind. The authoritative locked re-check
        (``_enforce_connector_seat_cap``) still runs afterwards to close the
        TOCTOU race.
        """
        max_connectors = workspace.effective_max_connectors
        if max_connectors <= 0:
            self._raise_seat_cap(max_connectors, 0)
        active_count = await self._count_active_connectors(workspace_id)
        if active_count >= max_connectors:
            self._raise_seat_cap(max_connectors, active_count)

    async def _enforce_connector_seat_cap(self, workspace: Workspace, workspace_id: UUID) -> None:
        max_connectors = workspace.effective_max_connectors

        # Zero-cap plans (Free: max_connectors == 0) can never provision, so deny
        # deterministically WITHOUT taking the advisory lock. Acquiring it first
        # would let lock contention / lock_timeout turn this guaranteed 403
        # (CONNECTOR-001) into a retriable 503 (CONNECTOR-002) for a request that
        # can never succeed — and waste a lock + count round-trip (PR #860 review).
        if max_connectors <= 0:
            self._raise_seat_cap(max_connectors, 0)

        # Issue #857: for a positive cap, acquire a per-workspace
        # ``pg_advisory_xact_lock`` before the count read so two concurrent
        # provisions for the last seat cannot both observe ``count < cap`` and
        # both insert (TOCTOU over-provision). The lock is xact-scoped, so the
        # caller's transaction — which extends from this gate through
        # ``db.flush()`` of the new connector — keeps the serialization across
        # the read-then-write. Mirrors the workspace-create cap (#677,
        # ``quota_service.py``) and the admin-bonus path (``admin.py``).
        await self._acquire_connector_seat_lock(workspace_id)

        active_count = await self._count_active_connectors(workspace_id)
        if active_count >= max_connectors:
            self._raise_seat_cap(max_connectors, active_count)

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

    async def list_connectors(self, workspace_id: UUID) -> list[WorkspaceConnector]:
        """Return all connectors for a workspace, newest first."""
        result = await self.db.execute(
            select(WorkspaceConnector)
            .where(WorkspaceConnector.workspace_id == workspace_id)
            .order_by(WorkspaceConnector.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_connector_for_dispatch(
        self, connector_type: str, external_team_id: str
    ) -> WorkspaceConnector | None:
        """Resolve the connector serving a platform team (worker dispatch key)."""
        result = await self.db.execute(
            select(WorkspaceConnector)
            .where(
                WorkspaceConnector.connector_type == connector_type,
                WorkspaceConnector.external_team_id == external_team_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def delete_connector(self, workspace_id: UUID, connector_id: UUID) -> bool:
        """Delete a connector: revoke its KMC write key + drop its resource.

        Revokes the workspace-scoped ``connector:{id}`` API key so the worker's
        next config fetch fails closed, then deletes the owning ``resources``
        row (CASCADE removes the connector + its resource token). The
        write-target context is intentionally preserved (it holds user data).
        Returns ``False`` if no matching connector exists in the workspace.
        """
        from models.auth import APIKey
        from models.resource import Resource
        from utils.datetime import utcnow

        result = await self.db.execute(
            select(WorkspaceConnector).where(
                WorkspaceConnector.id == connector_id,
                WorkspaceConnector.workspace_id == workspace_id,
            )
        )
        connector = result.scalar_one_or_none()
        if connector is None:
            return False

        await self.db.execute(
            update(APIKey)
            .where(
                APIKey.workspace_id == workspace_id,
                APIKey.name == f"connector:{connector_id}",
                APIKey.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        await self.db.execute(delete(Resource).where(Resource.id == connector.resource_pk))
        return True

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
