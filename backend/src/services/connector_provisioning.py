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
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.resource_tokens import ResourceTokenManager
from models.auth import Workspace
from models.resource import Resource, ResourceSchema, ResourceToken, WorkspaceConnector
from services.resource_lookup import resolve_resource_pk, upsert_resource
from utils.exceptions import ConflictError, MemoryCloudException, NotFoundException, ValidationError
from utils.logger import get_logger

logger = get_logger(__name__)

CONNECTOR_TYPES = frozenset({"slack", "discord", "teams"})
_RESOURCE_ID_RE = re.compile(r"^[a-z0-9_-]+$")


def _flatten_runtime_fields(value: dict[str, Any], prefix: str = "") -> set[str]:
    """Return dotted leaf names for secret-free audit logging."""
    fields: set[str] = set()
    for key, child in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(child, dict):
            fields.update(_flatten_runtime_fields(child, name))
        else:
            fields.add(name)
    return fields


def _runtime_field(value: dict[str, Any], dotted_name: str) -> object:
    """Read a dotted leaf from a normalized runtime document."""
    current: object = value
    for part in dotted_name.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


# Issue #910: canonical chat resource schema provisioned at connector
# registration. The ai-worker writes ``payload={"text": <llm_summary>, ...}`` on
# every ingest_event upsert; ``text`` is the single agreed fulltext field
# (confirmed with ai-worker 2026-06-03) so the indexer's _project_payload
# projects the summary into ``fulltext_content`` (and the vector path). Shape
# matches ``api.routes.resource_schema.FieldDefinition.model_dump()``. Lineage
# (source_uri / memory_details) stays in event_metadata, NOT the payload (#896).
_CANONICAL_CHAT_FIELD_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "text",
        "type": "text",
        "description": "Chat message / LLM summary fulltext content (ai-worker ingest payload).",
        "classification": "public",
        "index_hint": "fulltext+vector",
        "unit": None,
        "enum_values": None,
        "example": None,
        "required": False,
    }
]


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


@dataclass(frozen=True)
class ConnectorListItem:
    """One connector row for the workspace list view, paired with its public
    ``resource_id`` slug (resolved via a JOIN so the internal ``resource_pk``
    DB key is never exposed on the surface — #991). ``display_name`` (the
    joined resource's human-readable label) and ``context_name`` (the
    write-target context's display name) ride the same query (#1389)."""

    connector: WorkspaceConnector
    resource_id: str
    display_name: str | None = None
    context_name: str | None = None


@dataclass(frozen=True)
class KmcKeyRotationResult:
    """Result returned after rotating a connector's KMC write key (#892)."""

    plaintext_kmc_api_key: str
    expires_at: datetime
    config_version: int


@dataclass(frozen=True)
class ConnectorRuntimeUpdateResult:
    """Normalized runtime controls (None = cleared) and revision after an admin update."""

    runtime_config: dict[str, Any] | None
    config_version: int


@dataclass(frozen=True)
class ConnectorSettingsUpdateResult:
    """Post-update connector settings (#1376). The LLM bundle is write-only —
    only a presence flag is surfaced."""

    channel_ids: list[Any] | None
    litellm_virtual_key_id: str | None
    llm_config_present: bool
    locale: str | None
    config_version: int


# Sentinel distinguishing "field not provided" from an explicit null (clear)
# in the PATCH-semantics settings update (#1376).
_UNSET: Any = object()

# Bounds for tenant-writable channel selections (#1376): Slack channel ids are
# ~11 chars; the caps only exist to keep a hostile payload from bloating the
# JSONB column / worker config body.
_MAX_CHANNEL_IDS = 500
_MAX_CHANNEL_ID_CHARS = 64


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
        app_key: str = "default",
        runtime_config: dict[str, Any] | None = None,
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

        # #1377: strict write boundary for the worker Locale contract (see
        # WORKER_LOCALES) — a connector must never be born un-vendable.
        from models.worker_runtime import normalize_worker_locale

        try:
            locale = normalize_worker_locale(locale)
        except ValueError as ve:
            raise ValidationError(str(ve), field="locale") from ve

        # #1376 review: same un-vendable guard as the settings PATCH.
        self._validate_llm_config(llm_config)

        normalized_runtime_config = None
        if runtime_config is not None:
            from models.worker_runtime import WorkerRuntimeConfig

            normalized_runtime_config = WorkerRuntimeConfig.model_validate(
                runtime_config
            ).model_dump(mode="json")

        workspace = await self._get_workspace(workspace_id)

        # Non-default identities must already be active. ``default`` stays
        # compatible during the migration window while its signing secret is
        # still supplied by the worker environment — but an EXPLICITLY
        # disabled default is an admin revocation and must fail closed here
        # too, or the new connector would be born unusable (dispatch 410s).
        from services.worker_app_identity import WorkerAppIdentityService

        app_identity = await WorkerAppIdentityService(self.db).get_identity(connector_type, app_key)
        if app_key != "default":
            if (
                app_identity is None
                or app_identity.status != "active"
                or not app_identity.active_signing_secret_encrypted
            ):
                raise ValidationError(
                    "app_key must identify an active worker app identity",
                    field="app_key",
                )
        elif app_identity is not None and app_identity.status == "disabled":
            raise ValidationError(
                "The default worker app identity is disabled; re-enable it "
                "before binding new connectors",
                field="app_key",
            )

        # One app-qualified platform team maps to one connector — check BEFORE creating
        # any context so a duplicate is rejected without leaving orphan rows.
        # Falsy check (not `is not None`) also rejects empty-string team_id from
        # a malformed OAuth response. Do NOT include the conflicting connector_id —
        # it may belong to a different workspace (cross-tenant UUID disclosure).
        if external_team_id:
            await self._assert_team_unclaimed(
                workspace_id=workspace_id,
                connector_type=connector_type,
                app_key=app_key,
                external_team_id=external_team_id,
            )

        # ``auto_create_context_name`` goes through ContextService.create_context,
        # which COMMITS mid-flow — that would release the seat-cap advisory lock
        # before the connector insert and re-open the TOCTOU over-provision hole.
        # So for the auto-create path: non-locking pre-check → create+commit the
        # context → THEN take the lock for the authoritative gate (held through
        # the connector flush + the caller's commit). The legacy / existing-
        # context paths keep the original order (lock first) since they never
        # commit before the insert.
        # Track an auto-created context so we can clean it up on ANY downstream
        # failure: ContextService.create_context() COMMITS the context, so the
        # route-layer rollback cannot undo it. Without this, a seat-cap hit,
        # resource_id conflict, UNIQUE-on-team race at flush, key mint, or token
        # mint failure would all leave an orphan context with no connector.
        auto_created_context_id: UUID | None = None
        if auto_create_context_name:
            await self._check_seat_cap_available(workspace, workspace_id)
            resolved_context_id = await self._resolve_context(
                workspace=workspace,
                user_id=user_id,
                context_id=None,
                auto_create_context_name=auto_create_context_name,
            )
            auto_created_context_id = resolved_context_id
        else:
            await self._enforce_connector_seat_cap(workspace, workspace_id)
            resolved_context_id = await self._resolve_context(
                workspace=workspace,
                user_id=user_id,
                context_id=context_id,
                auto_create_context_name=None,
            )

        try:
            # For the auto-create path the authoritative locked seat-cap gate runs
            # here (after the context commit) so the advisory lock is held through
            # the connector flush below without an intervening commit releasing it.
            if auto_create_context_name:
                await self._enforce_connector_seat_cap(workspace, workspace_id)
                # Issue #887: a freshly auto-created connector context receives
                # external ingestion, so stamp it 'external' — the authoritative,
                # server-side trust signal that excludes it from behaviour-
                # influencing reads regardless of any client-supplied per-row
                # source_type (survives BYOK key leakage). Scoped to the
                # auto-create path; a bring-your-own existing context is the
                # user's own and is not silently re-tiered. Placed INSIDE the
                # try so a failure here triggers the orphan-context cleanup in
                # the except below (it would otherwise leak a committed context).
                from models.auth import CONTEXT_TRUST_TIER_EXTERNAL, Context

                await self.db.execute(
                    update(Context)
                    .where(Context.id == resolved_context_id)
                    .values(trust_tier=CONTEXT_TRUST_TIER_EXTERNAL)
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

            # Issue #910: provision the canonical chat resource schema so the
            # ai-worker's LLM summary (written as ``payload={"text": ...}`` on
            # ingest_event) lands in an indexed fulltext field. Without a schema
            # the indexer's _project_payload skips the summary entirely (lost
            # from search), and schema registration is owner/UI-only so the
            # worker can't self-bootstrap one.
            await self._ensure_chat_resource_schema(resource_pk, resource_id)

            connector = WorkspaceConnector(
                resource_pk=resource_pk,
                workspace_id=workspace_id,
                connector_type=connector_type,
                app_key=app_key,
                context_id=resolved_context_id,
                locale=locale,
                channel_ids=channel_ids,
                external_team_id=external_team_id,
                pii_guardrail_config=pii_guardrail_config,
                runtime_config=normalized_runtime_config,
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
            # #895: capture the one-time resource-token plaintext encrypted on
            # the connector so the worker config endpoint can return it for the
            # resource-ingest write path. resource_tokens only stores the hash.
            # Guarded by context (mirrors the kmc key): the worker config
            # endpoint 404s without a write-target context, so a context-less
            # connector is never worker-usable and need not store the token.
            if resolved_context_id is not None:
                connector.set_resource_token(plaintext_token)
                await self.db.flush()
        except Exception:
            # Delete the committed auto-created context so it is not orphaned.
            if auto_created_context_id is not None:
                await self._delete_orphan_context(auto_created_context_id)
            raise

        # #1350 review: initial runtime controls set at provisioning must be
        # auditable too — otherwise the changed-fields contract is bypassable
        # by setting values at creation instead of via PATCH. Names of the
        # fields that diverge from defaults only, never values.
        runtime_fields: list[str] = []
        if normalized_runtime_config is not None:
            from models.worker_runtime import WorkerRuntimeConfig as _WRC

            _defaults = _WRC().model_dump(mode="json")
            runtime_fields = sorted(
                key
                for key in _flatten_runtime_fields(normalized_runtime_config)
                if _runtime_field(normalized_runtime_config, key) != _runtime_field(_defaults, key)
            )

        logger.info(
            "workspace_connector_provisioned",
            connector_id=str(connector.id),
            connector_type=connector_type,
            resource_id=resource_id,
            resource_pk=str(resource_pk),
            workspace_id=str(workspace_id),
            user_id=user_id,
            runtime_fields=runtime_fields,
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

    async def _delete_orphan_context(self, context_id: UUID) -> None:
        """Hard-delete a context that was committed before a provisioning failure.

        ContextService.create_context() issues its own db.commit(), so the context
        row is durable before provision_connector finishes. If the seat-cap check
        subsequently fails, the already-committed context must be deleted explicitly
        — the route-layer rollback cannot undo a prior commit. Uses a new session
        connection via a raw Core DELETE so it commits independently.
        """
        from models.auth import Context

        try:
            # Roll back any half-applied work (e.g. a connector INSERT that hit a
            # UNIQUE violation at flush) so the session is clean before we delete
            # the already-committed context in its own transaction.
            await self.db.rollback()
            await self.db.execute(delete(Context).where(Context.id == context_id))
            await self.db.commit()
            logger.info("orphan_context_cleaned_up", context_id=str(context_id))
        except Exception:
            logger.error(
                "orphan_context_cleanup_failed",
                context_id=str(context_id),
                note="Manual cleanup required for orphaned context",
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

    async def list_connectors(self, workspace_id: UUID) -> list[ConnectorListItem]:
        """Return all connectors for a workspace, newest first.

        Each row is paired with its public ``resource_id`` slug via a single
        JOIN on ``resources`` (no per-row lookup / N+1), so the surface exposes
        the slug instead of the internal ``resource_pk`` DB key (#991). The
        human-readable identity for the list row rides the same query (#1389):
        ``display_name`` from the resource's label and ``context_name`` from
        the write-target context (outer join — context_id is nullable).
        """
        from models.auth import Context

        result = await self.db.execute(
            select(
                WorkspaceConnector,
                Resource.resource_id,
                Resource.name,
                # NULL-only fallback (service-layer coalesce precedent):
                # an empty-string display_name stays authoritative.
                func.coalesce(Context.display_name, Context.name),
            )
            .join(Resource, WorkspaceConnector.resource_pk == Resource.id)
            .outerjoin(Context, WorkspaceConnector.context_id == Context.id)
            .where(WorkspaceConnector.workspace_id == workspace_id)
            .order_by(WorkspaceConnector.created_at.desc())
        )
        return [
            ConnectorListItem(
                connector=connector,
                resource_id=resource_id,
                display_name=resource_name,
                context_name=context_name,
            )
            for connector, resource_id, resource_name, context_name in result.all()
        ]

    async def _assert_team_unclaimed(
        self,
        *,
        workspace_id: UUID,
        connector_type: str,
        app_key: str,
        external_team_id: str,
    ) -> None:
        """Reject binding a platform team that is already claimed.

        Two layers (#1360): the app-qualified exact match keeps one
        connector per ``(type, app_key, team)`` anywhere, and the
        cross-tenant guard blocks a team bound to ANOTHER workspace from
        being pre-bound here under a *different* ``app_key`` — the #1315
        app-qualification would otherwise let a second tenant route that
        team's future events to a workspace its owner never authorized.
        Same-workspace multi-app stays allowed (one tenant, several
        platform apps). Both arms raise the same fixed message — no
        cross-tenant existence disclosure beyond the conflict itself.
        """
        # Serialize concurrent claims on the same (type, team): the
        # cross-tenant arm below is a read-then-write check with NO unique
        # backstop (the index is app_key-qualified), so two workspaces
        # racing under different app_keys would both pass the SELECT. The
        # xact-scoped advisory lock holds until commit — same pattern as
        # the seat-cap lock.
        await self.db.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtext(f"wct:{connector_type}:{external_team_id}")
                )
            )
        )
        existing_team = await self.get_connector_for_dispatch(
            connector_type=connector_type,
            external_team_id=external_team_id,
            app_key=app_key,
        )
        if existing_team is not None:
            raise ConflictError(
                f"A {connector_type} connector for team '{external_team_id}' already exists.",
            )
        other_tenant = (
            await self.db.execute(
                select(WorkspaceConnector.id)
                .where(
                    WorkspaceConnector.connector_type == connector_type,
                    WorkspaceConnector.external_team_id == external_team_id,
                    WorkspaceConnector.workspace_id != workspace_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if other_tenant is not None:
            raise ConflictError(
                f"A {connector_type} connector for team '{external_team_id}' already exists.",
            )

    async def get_connector_for_dispatch(
        self, connector_type: str, external_team_id: str, app_key: str = "default"
    ) -> WorkspaceConnector | None:
        """Resolve the connector serving an app-qualified platform team."""
        result = await self.db.execute(
            select(WorkspaceConnector)
            .where(
                WorkspaceConnector.connector_type == connector_type,
                WorkspaceConnector.app_key == app_key,
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

    async def update_runtime_config(
        self,
        *,
        workspace_id: UUID,
        connector_id: UUID,
        runtime_config: dict[str, Any] | None,
        user_id: str,
        expected_config_version: int | None = None,
    ) -> ConnectorRuntimeUpdateResult:
        """Replace (or clear) normalized tenant controls and bump the revision.

        The workspace predicate and row lock are part of the authorization and
        monotonic-revision contract: a connector from another workspace is
        indistinguishable from a missing connector, and concurrent updates
        cannot lose a revision increment.

        ``runtime_config=None`` clears the stored block back to NULL — the
        "worker built-in defaults" state existing rows start in — so a tuned
        connector is not a one-way door (#1350 review). The full-document
        replacement semantics make lost updates possible from stale readers,
        so callers can pass ``expected_config_version`` (the version their
        snapshot came from) to fail the write with ConflictError instead of
        silently reverting a concurrent change.
        """
        connector = await self._get_connector_for_update(
            workspace_id, connector_id, expected_config_version
        )

        from models.worker_runtime import WorkerRuntimeConfig

        normalized_runtime_config = (
            None
            if runtime_config is None
            else WorkerRuntimeConfig.model_validate(runtime_config).model_dump(mode="json")
        )
        # from_stored (lenient) for the previous side: a drifted stored doc
        # must never make the repair PATCH itself 500 (#1350 review).
        previous = (
            WorkerRuntimeConfig.from_stored(connector.runtime_config) or WorkerRuntimeConfig()
        ).model_dump(mode="json")
        # Diff against effective values: clearing shows the fields returning
        # to worker defaults.
        effective_new = (
            normalized_runtime_config
            if normalized_runtime_config is not None
            else WorkerRuntimeConfig().model_dump(mode="json")
        )
        changed_fields = sorted(
            key
            for key in _flatten_runtime_fields(previous) | _flatten_runtime_fields(effective_new)
            if _runtime_field(previous, key) != _runtime_field(effective_new, key)
        )
        connector.runtime_config = normalized_runtime_config
        connector.config_version += 1
        await self.db.flush()

        logger.info(
            "workspace_connector_runtime_updated",
            connector_id=str(connector_id),
            workspace_id=str(workspace_id),
            updated_by=user_id,
            changed_fields=changed_fields,
            cleared=normalized_runtime_config is None,
            config_version=connector.config_version,
        )
        return ConnectorRuntimeUpdateResult(
            runtime_config=normalized_runtime_config,
            config_version=connector.config_version,
        )

    async def _get_connector_for_update(
        self,
        workspace_id: UUID,
        connector_id: UUID,
        expected_config_version: int | None,
    ) -> WorkspaceConnector:
        """Lock and version-check a connector for an admin update.

        Shared by the runtime and settings PATCH paths so the authorization
        invariant (workspace predicate — a cross-tenant connector is
        indistinguishable from a missing one) and the optimistic-lock
        contract cannot drift between them.

        Raises:
            NotFoundException: unknown or cross-workspace connector.
            ConflictError: stale ``expected_config_version``.
        """
        result = await self.db.execute(
            select(WorkspaceConnector)
            .where(
                WorkspaceConnector.id == connector_id,
                WorkspaceConnector.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        connector = result.scalar_one_or_none()
        if connector is None:
            raise NotFoundException("Connector", str(connector_id))
        if (
            expected_config_version is not None
            and connector.config_version != expected_config_version
        ):
            raise ConflictError(
                f"Connector config_version is {connector.config_version}, "
                f"expected {expected_config_version} — reload and retry."
            )
        return connector

    @staticmethod
    def _validate_llm_config(config: dict[str, Any] | None) -> None:
        """Reject an LLM bundle that could never vend (#1376 review).

        The vend hands the bundle to the worker verbatim, so a junk dict
        stored here reads as ``llm_config_present=true`` in the admin UI
        while the tenant stays un-vendable. ``provider`` and ``model`` are
        the universal minimum; ``api_key`` is intentionally NOT required
        (provider-dependent — e.g. local ollama has none) and extra keys
        pass through opaquely.
        """
        if config is None:
            return
        if not all(
            isinstance(config.get(key), str) and config[key].strip()
            for key in ("provider", "model")
        ):
            raise ValidationError(
                "llm_config requires non-empty string 'provider' and 'model' "
                "(api_key and extra keys are provider-dependent); pass null to clear",
                field="llm_config",
            )

    async def update_connector_settings(
        self,
        *,
        workspace_id: UUID,
        connector_id: UUID,
        user_id: str,
        expected_config_version: int | None = None,
        channel_ids: list[Any] | None = _UNSET,
        litellm_virtual_key_id: str | None = _UNSET,
        llm_config: dict[str, Any] | None = _UNSET,
        locale: str | None = _UNSET,
    ) -> ConnectorSettingsUpdateResult:
        """PATCH-update connector vend settings (#1376).

        Fields left at ``_UNSET`` are untouched; an explicit ``None`` clears.
        Repairs UI-created connectors born un-vendable (the create endpoint is
        the only other writer of these columns). Shares the runtime PATCH's
        contract: workspace-predicated ``SELECT FOR UPDATE``, optional
        ``expected_config_version`` optimistic lock (409 on staleness), and a
        ``config_version`` bump so the worker refetches.

        Raises:
            ValidationError: no field provided, malformed ``channel_ids``
                (empty list / blank or non-string ids are rejected so the
                stored "no channels" state has exactly one canonical shape —
                the explicit null the vend already coalesces to ``[]``), an
                un-vendable ``llm_config`` shape, or a locale outside the
                worker contract.
            NotFoundException: unknown or cross-workspace connector.
            ConflictError: stale ``expected_config_version``.
        """
        provided_names = sorted(
            name
            for name, value in (
                ("channel_ids", channel_ids),
                ("litellm_virtual_key_id", litellm_virtual_key_id),
                ("llm_config", llm_config),
                ("locale", locale),
            )
            if value is not _UNSET
        )
        if not provided_names:
            raise ValidationError(
                "Provide at least one of channel_ids, litellm_virtual_key_id, llm_config, locale",
                field="body",
            )

        if channel_ids is not _UNSET and channel_ids is not None:
            if (
                not channel_ids
                or len(channel_ids) > _MAX_CHANNEL_IDS
                or not all(
                    isinstance(cid, str) and cid.strip() and len(cid) <= _MAX_CHANNEL_ID_CHARS
                    for cid in channel_ids
                )
            ):
                raise ValidationError(
                    "channel_ids must be a non-empty list of non-blank channel "
                    f"id strings (max {_MAX_CHANNEL_IDS} ids, "
                    f"{_MAX_CHANNEL_ID_CHARS} chars each); pass null to clear",
                    field="channel_ids",
                )

        if llm_config is not _UNSET:
            self._validate_llm_config(llm_config)

        if locale is not _UNSET:
            from models.worker_runtime import normalize_worker_locale

            try:
                locale = normalize_worker_locale(locale)
            except ValueError as ve:
                raise ValidationError(str(ve), field="locale") from ve

        connector = await self._get_connector_for_update(
            workspace_id, connector_id, expected_config_version
        )

        if channel_ids is not _UNSET:
            connector.channel_ids = channel_ids
        if litellm_virtual_key_id is not _UNSET:
            connector.litellm_virtual_key_id = litellm_virtual_key_id
        if llm_config is not _UNSET:
            connector.set_llm_config(llm_config)
        if locale is not _UNSET:
            connector.locale = locale
        connector.config_version += 1
        await self.db.flush()

        # Field NAMES only — the llm_config bundle carries credentials.
        logger.info(
            "workspace_connector_settings_updated",
            connector_id=str(connector_id),
            workspace_id=str(workspace_id),
            updated_by=user_id,
            changed_fields=provided_names,
            config_version=connector.config_version,
        )
        return ConnectorSettingsUpdateResult(
            channel_ids=connector.channel_ids,
            litellm_virtual_key_id=connector.litellm_virtual_key_id,
            llm_config_present=bool(connector.llm_config_encrypted),
            locale=connector.locale,
            config_version=connector.config_version,
        )

    async def rotate_kmc_key(
        self,
        workspace_id: UUID,
        connector_id: UUID,
        user_id: str,
        expires_days: int = 365,
    ) -> KmcKeyRotationResult:
        """Revoke the current KMC write key and mint a replacement.

        Revokes the existing ``connector:{id}`` API key, mints a fresh
        workspace-scoped key with the given expiry, Fernet-encrypts it on
        the connector row, and bumps ``config_version`` so the worker
        re-fetches on its next poll.

        Returns:
            ``KmcKeyRotationResult`` with the one-time plaintext key, the new
            expiry, and the bumped config_version (so the caller need not
            re-query the connector).

        Raises:
            NotFoundException: if no matching connector exists in workspace.
            ValidationError: if the connector has no KMC key to rotate
                (registration flow was not completed).
        """
        from auth.api_keys import APIKeyManager
        from models.auth import APIKey
        from utils.datetime import utcnow
        from utils.hashing import sha256_hex

        result = await self.db.execute(
            select(WorkspaceConnector).where(
                WorkspaceConnector.id == connector_id,
                WorkspaceConnector.workspace_id == workspace_id,
            )
        )
        connector = result.scalar_one_or_none()
        if connector is None:
            raise NotFoundException("Connector", str(connector_id))
        current_key = connector.get_kmc_api_key()
        if not current_key:
            raise ValidationError(
                "Connector has no KMC write key; register with a write-target context first"
            )

        # Locate the exact active APIKey row backing the stored key (matched by
        # key_hash, NOT name — create_key only enforces name uniqueness per
        # (user_id, workspace_id), so a name match could hit an unrelated user's
        # key). Fetching the row lets us (a) fail loudly if the stored key is
        # stale instead of silently minting a duplicate, and (b) preserve the
        # original owner's user_id on the replacement so rotation by a different
        # admin doesn't transfer key ownership.
        old_key_result = await self.db.execute(
            select(APIKey).where(
                APIKey.workspace_id == workspace_id,
                APIKey.key_hash == sha256_hex(current_key),
                APIKey.revoked_at.is_(None),
            )
        )
        old_key = old_key_result.scalar_one_or_none()
        if old_key is None:
            # Stored key matches no active APIKey row (Fernet secret rotated,
            # external revocation, or DB drift). Fail with a clear diagnostic
            # rather than letting create_key surface an opaque 500.
            raise ValidationError(
                "Connector's stored KMC key matches no active API key; "
                "re-register the connector to mint a fresh key"
            )
        owner_user_id = old_key.user_id

        # Revoke the old key immediately — no grace period for v1. Operators
        # should schedule rotation during a maintenance window or worker pause.
        old_key.revoked_at = utcnow()

        # Mint replacement key under the ORIGINAL owner (not the rotating admin)
        # so per-user key listings stay correct. create_key computes and stores
        # expires_at on the row; reuse that exact value for the connector column
        # so the two never drift (a second utcnow() would differ by microseconds).
        plaintext_new_key, new_key_row = await APIKeyManager(self.db).create_key(
            name=f"connector:{connector_id}",
            user_id=owner_user_id,
            workspace_id=workspace_id,
            expires_days=expires_days,
        )
        connector.set_kmc_api_key(plaintext_new_key)
        connector.kmc_api_key_expires_at = new_key_row.expires_at
        connector.config_version = connector.config_version + 1
        await self.db.flush()

        logger.info(
            "connector_kmc_key_rotated",
            connector_id=str(connector_id),
            workspace_id=str(workspace_id),
            rotated_by=user_id,
            key_owner=owner_user_id,
            expires_days=expires_days,
        )
        return KmcKeyRotationResult(
            plaintext_kmc_api_key=plaintext_new_key,
            expires_at=new_key_row.expires_at,
            config_version=connector.config_version,
        )

    async def _get_connector_for_resource(
        self,
        resource_pk: UUID,
    ) -> WorkspaceConnector | None:
        result = await self.db.execute(
            select(WorkspaceConnector).where(WorkspaceConnector.resource_pk == resource_pk)
        )
        return result.scalar_one_or_none()

    async def _ensure_chat_resource_schema(self, resource_pk: UUID, resource_id: str) -> None:
        """Provision the canonical chat resource schema (v1) if none exists (#910).

        Idempotent: a re-provision (or a resource that already carries a schema,
        e.g. a manually registered one) is left untouched so an operator's custom
        schema is never clobbered. Only the first registration seeds the
        canonical ``text`` fulltext field the worker writes to.

        The pre-check skips seeding when ANY schema version already exists; the
        insert itself is ``ON CONFLICT DO NOTHING`` on the partial unique index
        ``uq_resource_schemas_version (resource_pk, schema_version)`` so two
        concurrent first-provisions for the same resource can't raise a UNIQUE
        violation (the loser's insert is a no-op — the desired end state holds).
        """
        existing = (
            await self.db.execute(
                select(ResourceSchema.id).where(ResourceSchema.resource_pk == resource_pk).limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        await self.db.execute(
            pg_insert(ResourceSchema)
            .values(
                resource_pk=resource_pk,
                resource_id=resource_id,
                schema_version=1,
                field_definitions=_CANONICAL_CHAT_FIELD_DEFINITIONS,
            )
            .on_conflict_do_nothing(
                index_elements=["resource_pk", "schema_version"],
                index_where=text("resource_pk IS NOT NULL"),
            )
        )


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
